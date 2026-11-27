import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "scripts"))

from api.handler import lambda_handler
from delete_event import ensure_quiet, remove_event


class EventSearchTests(unittest.TestCase):
    def test_search_uses_saved_name_or_exact_id_and_projects_metadata(self):
        catalog = [
            {"event_id": "luna-live", "name": "Luna Vale Concert", "conflicts": 3},
            {"event_id": "luna-encore", "name": "Luna Vale Encore"},
            {"event_id": "concert-1"},
        ]
        database = Mock()
        database.list_events.return_value = catalog
        def search(name):
            return lambda_handler({
                "httpMethod": "GET", "resource": "/events",
                "queryStringParameters": {"name": name},
            }, None)
        with patch("api.events.database", return_value=database):
            self.assertEqual(search("  ")["statusCode"], 400)
            database.list_events.assert_not_called()
            response = search("lUNA vALE cONCERT")
            self.assertEqual(response["statusCode"], 200)
            self.assertEqual(json.loads(response["body"])["events"], [
                {"event_id": "luna-live", "name": "Luna Vale Concert"},
            ])
            self.assertEqual(len(json.loads(search("luna")["body"])["events"]), 2)
            self.assertEqual(json.loads(search("concert-1")["body"])["events"][0]["name"], "concert-1")

    def test_legacy_http_search_dispatches(self):
        with patch("api.events.database") as database:
            database.return_value.list_events.return_value = []
            response = lambda_handler({
                "routeKey": "$default", "rawPath": "/events",
                "requestContext": {"http": {"method": "GET"}},
                "queryStringParameters": {"name": "anything"},
            }, None)
        self.assertEqual(json.loads(response["body"]), {"events": [], "truncated": False})


class DeleteEventTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(account="000000000000", prefix="ticketing",
                                       region="us-east-2", event="old-concert", execute=False)
        self.snapshot = (
            {"event_id": {"S": "old-concert"}, "created_at_ms": {"N": "100"}},
            [{"seat_id": {"S": "A-1"}, "status": {"S": "sold"}}],
            [{"order_id": {"S": "example"}, "status": {"S": "confirmed"}}],
        )

    def test_preview_and_cancel_produce_no_writes(self):
        with patch("delete_event.aws", return_value={"Account": self.args.account}), patch(
            "delete_event.read_event", return_value=self.snapshot
        ), patch("delete_event.dynamodb") as dynamodb, patch(
            "builtins.input", return_value="not-the-event"
        ) as confirm, redirect_stdout(io.StringIO()):
            remove_event(self.args)
            confirm.assert_not_called()
            self.args.execute = True
            with self.assertRaisesRegex(SystemExit, "Cancelled"):
                remove_event(self.args)
        dynamodb.assert_not_called()

    def test_deletion_is_single_transaction_on_the_chosen_keys(self):
        self.args.execute = True
        with patch("delete_event.aws", return_value={"Account": self.args.account}), patch(
            "delete_event.read_event", side_effect=[self.snapshot, self.snapshot, (None, [], [])]
        ), patch("delete_event.dynamodb") as dynamodb, patch(
            "builtins.input", return_value="DELETE old-concert"
        ), redirect_stdout(io.StringIO()) as output:
            remove_event(self.args)
        dynamodb.assert_called_once()
        self.assertEqual(dynamodb.call_args.args[2], "transact-write-items")
        actions = [action["Delete"] for action in dynamodb.call_args.args[3]["TransactItems"]]
        self.assertEqual([action["TableName"] for action in actions],
                         ["ticketing-orders", "ticketing-seats", "ticketing-events"])
        self.assertEqual(actions[-1]["Key"], {"event_id": {"S": "old-concert"}})
        self.assertIn("Deleted and verified", output.getvalue())

    def test_active_seat_and_pending_work_block_deletion(self):
        with self.assertRaisesRegex(SystemExit, "active seat holds"):
            ensure_quiet([{"status": {"S": "held"}, "hold_expires_at_ms": {"N": "1001"}}], [], 1000)
        with self.assertRaisesRegex(SystemExit, "unfinished orders"):
            ensure_quiet([], [{"status": {"S": "pending"}}], 1000)
        with self.assertRaisesRegex(SystemExit, "unfinished receipt"):
            ensure_quiet([], [{"status": {"S": "confirmed"}, "receipt_status": {"S": "sending"}}], 1000)
