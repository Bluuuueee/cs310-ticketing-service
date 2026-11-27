import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from api.handler import lambda_handler
from common.db import Database
from common.tickets import make_code, verify_code
from worker import handler as worker

ORDER_ID = "440bb2a12a664bdca01d31c3a5f18ef8"
ORDER = {
    "order_id": ORDER_ID, "event_id": "concert-1", "hold_id": "h-111111111111",
    "buyer_id": "buyer", "seats": ["A-5", "A-6"], "status": "pending", "paid": False,
}


class Canceled(Exception):
    def __init__(self, *codes):
        self.response = {"CancellationReasons": [{"Code": code} for code in codes]}


class ConditionalFailed(Exception):
    pass


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.db.list_seats.return_value = [
            {"seat_id": seat, "hold_id": ORDER["hold_id"], "holder": "buyer",
             "row_index": 0, "seat_num": index}
            for index, seat in enumerate(ORDER["seats"], 5)
        ]
        self.db.get_order.return_value = ORDER
        self.db.get_event.return_value = {"mode": "correct"}
        self.db.create_order.side_effect = [True, False]
        for target in ("api.checkout.database", "api.orders.database", "worker.handler.database"):
            patcher = patch(target, return_value=self.db)
            patcher.start()
            self.addCleanup(patcher.stop)

    def submit(self, buyer="buyer", key=ORDER_ID):
        return lambda_handler({
            "routeKey": "POST /events/{event_id}/holds/{hold_id}/checkout",
            "pathParameters": {"event_id": ORDER["event_id"], "hold_id": ORDER["hold_id"]},
            "headers": {"X-Buyer-Id": buyer, "Idempotency-Key": key},
        }, None)

    @patch("api.checkout.enqueue")
    def test_checkout_reuses_order_and_enqueues_once(self, enqueue):
        for _ in range(2):
            result = self.submit()
            self.assertEqual(result["statusCode"], 202)
            self.assertEqual(json.loads(result["body"]), {"order_id": ORDER_ID, "status": "pending"})
        enqueue.assert_called_once_with(ORDER_ID)

    @patch("api.checkout.enqueue")
    def test_checkout_rejects_wrong_owner_and_malformed_key(self, enqueue):
        self.assertEqual(self.submit(buyer="other")["statusCode"], 403)
        self.assertEqual(self.submit(key="bad")["statusCode"], 400)
        enqueue.assert_not_called()
        self.db.create_order.assert_not_called()

    def test_polling_exposes_order_and_handles_missing_order(self):
        event = {"routeKey": "GET /orders/{order_id}", "pathParameters": {"order_id": ORDER_ID}}
        response = lambda_handler(event, None)
        self.assertEqual(json.loads(response["body"]), {**ORDER, "tickets": [], "fail_reason": None})
        self.db.get_order.return_value = None
        self.assertEqual(lambda_handler(event, None)["statusCode"], 404)

    @patch.dict(os.environ, {"TICKET_SECRET": "test-secret"})
    @patch("worker.handler.simulate_payment")
    def test_worker_issues_deterministic_codes_and_ignores_confirmed_delivery(self, payment):
        self.db.get_order.side_effect = [ORDER, {**ORDER, "status": "confirmed"}]
        event = {"Records": [{"body": json.dumps({"order_id": ORDER_ID})}]}
        worker.lambda_handler(event, None)
        worker.lambda_handler(event, None)
        payment.assert_called_once()
        self.db.confirm_order.assert_called_once()
        tickets = self.db.confirm_order.call_args.args[1]
        for ticket in tickets:
            seat = ticket["seat_id"]
            code = ticket["code"]
            self.assertEqual(code, make_code("test-secret", "concert-1", seat, ORDER_ID))
            self.assertEqual(verify_code("test-secret", code), ("concert-1", seat, ORDER_ID))
            self.assertIsNone(verify_code("wrong-secret", code))

    @patch("worker.handler.simulate_payment", side_effect=RuntimeError("payment retry"))
    def test_worker_raises_retryable_payment_error_before_writing(self, payment):
        with self.assertRaisesRegex(RuntimeError, "payment retry"):
            worker.process_order(ORDER_ID)
        self.db.confirm_order.assert_not_called()


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.exceptions.TransactionCanceledException = Canceled
        self.client.exceptions.ConditionalCheckFailedException = ConditionalFailed
        serializer = Mock()
        serializer.serialize.side_effect = lambda value: value
        self.db = Database(self.client, "ticketing", serializer, Mock())
        self.tickets = [{"seat_id": seat, "code": "example"} for seat in ORDER["seats"]]

    def test_order_condition_is_last_and_naive_retains_it(self):
        for naive in (False, True):
            self.db.confirm_order(ORDER, self.tickets, 1000, naive)
            items = self.client.transact_write_items.call_args.kwargs["TransactItems"]
            self.assertEqual(items[-1]["Update"]["ConditionExpression"], "#st = :pending")
            self.assertEqual(items[-1]["Update"]["TableName"], "ticketing-orders")
            for item in items[:-1]:
                self.assertEqual("ConditionExpression" in item["Update"], not naive)

    def test_duplicate_has_priority_over_seat_failure(self):
        self.client.transact_write_items.side_effect = Canceled("ConditionalCheckFailed", "None", "ConditionalCheckFailed")
        self.db.confirm_order(ORDER, self.tickets, 1000, False)
        self.client.update_item.assert_not_called()

    def test_expired_hold_marks_pending_order_failed(self):
        self.client.transact_write_items.side_effect = Canceled("None", "ConditionalCheckFailed", "None")
        self.db.confirm_order(ORDER, self.tickets, 1000, False)
        update = self.client.update_item.call_args.kwargs
        self.assertEqual(update["ConditionExpression"], "#st = :pending")
        self.assertEqual(update["ExpressionAttributeValues"][":reason"], "hold_expired")

    def test_transaction_conflict_is_retryable(self):
        self.client.transact_write_items.side_effect = Canceled("TransactionConflict", "None", "None")
        with self.assertRaises(Canceled):
            self.db.confirm_order(ORDER, self.tickets, 1000, False)
        self.client.update_item.assert_not_called()
