import json
import os
from email import policy
from email.parser import BytesParser
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from api.handler import lambda_handler
from common.tickets import make_code
from worker.handler import lambda_handler as lambda_handler_worker
from worker.handler import process_order
from worker.receipts import build_receipt, send_receipt

RECIPIENT = "demo@example.com"
SENDER = "sender@example.com"
ORDER = {
    "order_id": "440bb2a12a664bdca01d31c3a5f18ef8",
    "event_id": "concert-1", "hold_id": "h-111111111111",
    "buyer_id": "buyer", "seats": ["A-5", "A-6"], "status": "confirmed", "paid": True,
    "updated_at_ms": 1795694924572, "receipt_email": RECIPIENT, "receipt_status": "pending",
}


class ReceiptsTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        environment = patch.dict(os.environ, {
            "SES_FROM_EMAIL": SENDER, "SES_TEST_RECIPIENT": RECIPIENT,
            "TICKET_SECRET": "test-secret",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_calendar_attachment_and_confirmation_body(self):
        tickets = [
            {"seat_id": seat, "code": make_code("test-secret", ORDER["event_id"], seat, ORDER["order_id"])}
            for seat in ORDER["seats"]
        ]
        receipt = build_receipt(SENDER, RECIPIENT, {**ORDER, "tickets": tickets}, {})
        parsed = BytesParser(policy=policy.default).parsebytes(receipt)
        self.assertEqual(parsed["Subject"], "Booking confirmed: Luna Vale Concert")
        body = parsed.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("Seats: A-5, A-6", body)
        for ticket in tickets:
            self.assertIn(f"{ticket['seat_id']}: {ticket['code']}", body)
        attachment, = parsed.iter_attachments()
        self.assertEqual(attachment.get_filename(), "event.ics")
        self.assertEqual(attachment.get_content_type(), "text/calendar")
        data = attachment.get_payload(decode=True)
        for ticket in tickets:
            self.assertNotIn(ticket["code"].encode(), data)
        self.assertIn(b"DTSTART:20261213T020000Z\r\n", data)
        self.assertIn(b"DTEND:20261213T040000Z\r\n", data)
        self.assertIn(b"SUMMARY:Luna Vale Concert\r\n", data)

    def test_checkout_saves_allowlisted_email_and_get_order_keeps_it_private(self):
        self.db.list_seats.return_value = [
            {"seat_id": "A-5", "hold_id": ORDER["hold_id"], "holder": "buyer",
             "row_index": 0, "seat_num": 5},
        ]
        self.db.get_order.return_value = ORDER
        event = {
            "routeKey": "POST /events/{event_id}/holds/{hold_id}/checkout",
            "pathParameters": {"event_id": "concert-1", "hold_id": ORDER["hold_id"]},
            "headers": {"X-Buyer-Id": "buyer", "Idempotency-Key": ORDER["order_id"]},
        }
        with patch("api.checkout.database", return_value=self.db), patch("api.checkout.enqueue"):
            for email in ("bad@example.com", "bad\r\n@example.com"):
                response = lambda_handler({**event, "body": json.dumps({"email": email})}, None)
                self.assertEqual(response["statusCode"], 400)
            self.db.create_order.assert_not_called()
            response = lambda_handler({**event, "body": json.dumps({"email": RECIPIENT.upper()})}, None)
            self.assertEqual(response["statusCode"], 202)
        created = self.db.create_order.call_args.args[0]
        self.assertEqual((created["receipt_email"], created["receipt_status"]), (RECIPIENT, "pending"))
        with patch("api.orders.database", return_value=self.db):
            response = lambda_handler({
                "routeKey": "GET /orders/{order_id}", "pathParameters": {"order_id": ORDER["order_id"]},
            }, None)
        self.assertNotIn("receipt_email", json.loads(response["body"]))

    @patch("worker.receipts.ses_client")
    def test_duplicate_cannot_reclaim_and_resend_receipt(self, ses):
        self.db.claim_receipt.side_effect = [True, False]
        send_receipt(self.db, ORDER, {})
        send_receipt(self.db, ORDER, {})
        ses.return_value.send_email.assert_called_once()
        self.db.finish_receipt.assert_called_once_with(ORDER["order_id"], "sent", None)

    @patch("worker.receipts.ses_client")
    def test_mail_failure_records_status_without_reopening_order(self, ses):
        self.db.claim_receipt.return_value = True
        rejection = RuntimeError(f"Private service message about {RECIPIENT}")
        rejection.response = {"Error": {"Code": "MessageRejected", "Message": str(rejection)}}
        ses.return_value.send_email.side_effect = rejection
        with self.assertLogs("worker.receipts", level="ERROR") as logs:
            send_receipt(self.db, ORDER, {})
        self.assertIn("MessageRejected", logs.output[0])
        self.assertNotIn(RECIPIENT, logs.output[0])
        self.db.finish_receipt.assert_called_once_with(ORDER["order_id"], "failed", "MessageRejected")

    @patch("worker.handler.send_receipt")
    @patch("worker.handler.simulate_payment")
    def test_worker_only_notifies_after_reading_confirmed_order(self, payment, send):
        self.db.get_event.return_value = {"mode": "correct"}
        self.db.get_order.side_effect = [
            {**ORDER, "status": "pending"}, {**ORDER, "status": "failed"},
            {**ORDER, "status": "pending"}, ORDER,
        ]
        with patch("worker.handler.database", return_value=self.db):
            process_order(ORDER["order_id"])
            send.assert_not_called()
            process_order(ORDER["order_id"])
        send.assert_called_once_with(self.db, ORDER, {"mode": "correct"})

    def test_operator_retry_targets_only_confirmed_failed_receipt(self):
        token = "a" * 32
        retry = {"receipt_retry": {"order_id": ORDER["order_id"], "token": token}}
        rejected = {**ORDER, "receipt_status": "sent"}
        failed = {**ORDER, "receipt_status": "failed"}
        next_failure = {**failed, "receipt_error": "MessageRejected"}
        event = {"mode": "correct"}
        self.db.get_event.return_value = event
        with patch("worker.handler.database", return_value=self.db), patch(
            "worker.handler.send_receipt"
        ) as send, patch("worker.handler.simulate_payment") as payment:
            self.db.get_order.side_effect = [rejected, failed, next_failure]
            self.assertEqual(lambda_handler_worker(retry, None), {"receipt": "not_eligible"})
            send.assert_not_called()
            self.assertEqual(lambda_handler_worker(retry, None), {
                "receipt": "failed", "error": "MessageRejected",
            })
            send.assert_called_once_with(self.db, failed, event, retry_token=token)
            payment.assert_not_called()
            self.db.confirm_order.assert_not_called()
