"""Exercise the REST event shape and both CloudFormation entry points."""

import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "server"))
sys.path.insert(0, str(PROJECT / "scripts"))
from api.handler import lambda_handler
from deploy_rest import attach
from rest_gateway import METHODS, rest_gateway
from setup import template


class RestTests(unittest.TestCase):
    def test_rest_event_preserves_route_and_path_parameters(self):
        event = {
            "httpMethod": "GET",
            "resource": "/events/{event_id}/seats",
            "path": "/events/concert-1/seats",
            "requestContext": {"stage": "prod"},
            "pathParameters": {"event_id": "concert-1"},
        }
        with patch("api.seats.database") as database:
            database.return_value.get_event.return_value = None
            response = lambda_handler(event, None)
        self.assertEqual(response["statusCode"], 404)
        self.assertEqual(json.loads(response["body"])["error"], "event_not_found")
        database.return_value.get_event.assert_called_once_with("concert-1")

    def test_rest_template_uses_proxy_and_only_adds_gateway_resources(self):
        resources, url = rest_gateway("ticketing", "ticketing-api", "arn:test:demo")
        self.assertEqual(resources["RestApi"]["Type"], "AWS::ApiGateway::RestApi")
        paths = resources["RestApi"]["Properties"]["Body"]["paths"]
        self.assertEqual(set(paths), set(METHODS))
        for path, method in METHODS.items():
            integration = paths[path][method]["x-amazon-apigateway-integration"]
            self.assertEqual((integration["type"], integration["httpMethod"]), ("aws_proxy", "POST"))
        stage = resources["RestStage"]["Properties"]
        self.assertEqual(stage["StageName"], "prod")
        self.assertIn(stage["DeploymentId"]["Ref"], resources)
        self.assertTrue(url["Fn::Sub"].endswith("/prod"))
        self.assertIn("RestApi", template("ticketing")["Resources"])
        self.assertIn("HttpApi", template("ticketing", "http")["Resources"])

    def test_config_switch_waits_for_real_verification(self):
        args = argparse.Namespace(account="000000000000", region="us-east-2",
                                  profile="cs310", prefix="ticketing")
        legacy = {"api_url": "https://old.example", "region": args.region, "prefix": args.prefix}
        rest = "https://new.example/prod"

        def aws(args, *command, stream=False):
            return {
                ("sts", "get-caller-identity"): {"Account": args.account},
                ("lambda", "get-function"): "arn:test:demo",
                ("cloudformation", "describe-stacks"): {
                    "Stacks": [{"Outputs": [{"OutputKey": "ApiUrl", "OutputValue": rest}]}],
                },
            }.get(command[:2])

        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config/ticketing.json"
            config.parent.mkdir()
            config.write_text(json.dumps(legacy), encoding="utf-8")
            with patch("deploy_rest.ROOT", root), patch("deploy_rest.aws", aws), patch(
                "deploy_rest.build", return_value=root / "api.zip"
            ), patch("deploy_rest.verify_api") as api_probe, patch(
                "deploy_rest.verify_web", side_effect=[RuntimeError("failed"), None]
            ) as web_probe:
                with self.assertRaises(RuntimeError):
                    attach(args)
                self.assertEqual(json.loads(config.read_text(encoding="utf-8")), legacy)
                attach(args)
                updated = json.loads(config.read_text(encoding="utf-8"))
                self.assertEqual(updated["api_url"], rest)
                self.assertEqual(updated["previous_http_api_url"], legacy["api_url"])
                self.assertEqual(updated["gateway_type"], "rest")
                self.assertEqual(api_probe.call_count, 2)
                self.assertEqual(web_probe.call_count, 2)
