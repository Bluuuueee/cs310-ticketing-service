import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from api.handler import lambda_handler
from api.holds import best_block
from common.db import Database, SeatClaimConflict


def sample_seats():
    return [
        {"seat_id": f"{row}-{number}", "row": row, "row_index": index,
         "seat_num": number, "status": "available"}
        for index, row in enumerate("ABCDE") for number in range(1, 11)
    ]


class SeatTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.db.get_event.return_value = {
            "mode": "correct", "hold_seconds": 30, "rows": 5,
            "seats_per_row": 10, "capacity": 50, "conflicts": 2,
        }
        self.db.list_seats.return_value = sample_seats()
        self.db.list_orders.return_value = []
        for module in ("holds", "seats", "stats"):
            for target, value in (("database", self.db), ("now_ms", 1000)):
                mocked = patch(f"api.{module}.{target}", return_value=value)
                mocked.start()
                self.addCleanup(mocked.stop)

    def call(self, route, body=None):
        result = lambda_handler({
            "routeKey": route, "pathParameters": {"event_id": "concert-1"},
            "headers": {"X-Buyer-Id": "test"},
            "body": json.dumps(body) if body is not None else None,
        }, None)
        return result["statusCode"], json.loads(result["body"])

    def test_best_block_for_all_group_sizes_and_no_crossing_gaps(self):
        for size, expected in {
            1: ["A-5"], 2: ["A-5", "A-6"],
            3: ["A-4", "A-5", "A-6"], 4: ["A-4", "A-5", "A-6", "A-7"],
        }.items():
            self.assertEqual(best_block(sample_seats(), size, 10, 1000), expected)
        self.assertEqual(best_block(sample_seats()[0:4:2], 2, 10, 1000), [])

    def test_auto_hold_rereads_after_a_conflict(self):
        new_view = sample_seats()
        for seat in new_view[4:6]:
            seat.update(status="held", hold_expires_at_ms=2000)
        self.db.list_seats.side_effect = [sample_seats(), new_view]
        self.db.claim_seats.side_effect = [SeatClaimConflict, None]
        status, body = self.call("POST /events/{event_id}/holds", {"party_size": 2})
        self.assertEqual((status, body["seats"], body["expires_at_ms"]), (201, ["A-3", "A-4"], 31000))
        self.db.record_conflict.assert_called_once_with("concert-1")

    def test_explicit_hold_conflict_uses_one_attempt(self):
        self.db.claim_seats.side_effect = SeatClaimConflict
        status, body = self.call("POST /events/{event_id}/holds", {"seats": ["A-3"]})
        self.assertEqual((status, body["error"]), (409, "seats_taken"))
        self.db.claim_seats.assert_called_once()

    def test_hold_retry_limit_and_duplicate_ids(self):
        self.db.claim_seats.side_effect = SeatClaimConflict
        self.assertEqual(self.call("POST /events/{event_id}/holds", {"party_size": 2})[0], 409)
        self.assertEqual(self.db.claim_seats.call_count, 3)
        status, body = self.call("POST /events/{event_id}/holds", {"seats": ["A-3", "A-3"]})
        self.assertEqual((status, body["error"]), (400, "invalid_seats"))

    def test_reads_hide_expired_holds_and_count_double_bookings(self):
        seat = self.db.list_seats.return_value[0]
        seat.update(status="held", holder="expired", hold_expires_at_ms=1000)
        self.db.list_orders.return_value = [
            {"status": "confirmed", "seats": ["A-2", "A-3"]},
            {"status": "confirmed", "seats": ["A-2"]},
            {"status": "pending", "seats": ["A-2"]},
        ]
        _, body = self.call("GET /events/{event_id}/seats")
        self.assertEqual((body["seats"][0]["status"], body["seats"][0]["holder"]), ("available", None))
        _, body = self.call("GET /events/{event_id}/stats")
        self.assertEqual((body["tickets_issued"], body["double_booked_seats"]), (3, ["A-2"]))

    def test_dynamodb_claim_batches_conditions_and_naive_updates_individually(self):
        client = Mock()
        serializer = Mock()
        serializer.serialize.side_effect = lambda value: value
        db = Database(client, "ticketing", serializer, Mock())
        db.claim_seats("concert-1", ["A-5", "A-6"], "test", "h-id", 1000, 31000, False)
        items = client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(items), 2)
        self.assertTrue(all("hold_expires_at_ms <= :now" in item["Update"]["ConditionExpression"]
                            for item in items))
        client.update_item.assert_not_called()
        db.claim_seats("concert-1", ["A-5", "A-6"], "test", "h-id", 1000, 31000, True)
        self.assertEqual(client.update_item.call_count, 2)
        self.assertNotIn("ConditionExpression", client.update_item.call_args.kwargs)
