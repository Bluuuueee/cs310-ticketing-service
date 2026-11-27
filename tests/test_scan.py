import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from api.handler import lambda_handler
from common.db import Database
from common.tickets import make_code

ORDER = "440bb2a12a664bdca01d31c3a5f18ef8"
SECRET = "test-secret"


class ConditionalFailed(Exception):
    pass


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.code = make_code(SECRET, "concert-1", "A-5", ORDER)
        for patcher in (
            patch.dict(os.environ, {"TICKET_SECRET": SECRET}),
            patch("api.scan.database", return_value=self.db),
            patch("api.scan.now_ms", return_value=1000),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def scan(self, body):
        result = lambda_handler({"routeKey": "POST /tickets/scan", "body": json.dumps(body)}, None)
        return result["statusCode"], json.loads(result["body"])

    def test_missing_and_tampered_codes_do_not_call_db(self):
        status, body = self.scan({})
        self.assertEqual((status, body["error"]), (400, "missing_code"))
        message, sig = self.code.rsplit("|", 1)
        replaced = "B" if sig[0] == "A" else "A"
        self.assertEqual(self.scan({"code": f"{message}|{replaced}{sig[1:]}"}),
                         (200, {"result": "INVALID"}))
        self.assertEqual(self.db.mock_calls, [])

    def test_admit_and_replay_preserve_first_scan_metadata(self):
        self.db.use_ticket.side_effect = [True, False]
        self.db.get_seat.return_value = {"used_gate": "north", "used_at_ms": 1000}
        self.assertEqual(self.scan({"code": self.code, "gate": "north"}),
                         (200, {"result": "ADMIT", "seat_id": "A-5"}))
        self.db.use_ticket.assert_called_once_with("concert-1", "A-5", ORDER, "north", 1000)
        self.db.get_seat.assert_not_called()
        self.assertEqual(self.scan({"code": self.code, "gate": "south"}), (200, {
            "result": "ALREADY_USED", "seat_id": "A-5",
            "first_gate": "north", "first_used_at_ms": 1000,
        }))

    def test_failed_condition_without_metadata_follows_contract(self):
        self.db.use_ticket.return_value = False
        self.db.get_seat.return_value = None
        self.assertEqual(self.scan({"code": self.code}), (200, {
            "result": "ALREADY_USED", "seat_id": "A-5",
            "first_gate": None, "first_used_at_ms": None,
        }))

    def test_db_uses_one_conditional_update_and_strong_readback(self):
        client = Mock()
        client.exceptions.ConditionalCheckFailedException = ConditionalFailed
        serializer = Mock()
        serializer.serialize.side_effect = lambda value: value
        db = Database(client, "ticketing", serializer, Mock())
        self.assertTrue(db.use_ticket("concert-1", "A-5", ORDER, "north", 1000))
        client.update_item.assert_called_once()
        args = client.update_item.call_args.kwargs
        self.assertEqual(args["ConditionExpression"], "ticket_status = :valid AND order_id = :oid")
        client.update_item.side_effect = ConditionalFailed
        self.assertFalse(db.use_ticket("concert-1", "A-5", ORDER, "south", 1001))
        client.get_item.return_value = {}
        db.get_seat("concert-1", "A-5")
        self.assertTrue(client.get_item.call_args.kwargs["ConsistentRead"])
