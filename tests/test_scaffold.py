import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

from scripts.package import build

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from api.handler import lambda_handler as api_handler
from worker.handler import lambda_handler as worker_handler


class ScaffoldTests(unittest.TestCase):
    def test_unknown_route(self):
        response = api_handler({"routeKey": "GET /unknown"}, None)
        self.assertEqual(response["statusCode"], 404)
        self.assertEqual(json.loads(response["body"])["error"], "not_found")

    def test_request_json_and_base64(self):
        route = {"routeKey": "POST /tickets/scan"}
        encoded = base64.b64encode(b'{"party_size": 2}').decode("ascii")
        for body, is_encoded, status in (
            ('{"party_size": 2}', False, 400),
            (encoded, True, 400),
            ("{", False, 400),
            ("invalid!", True, 400),
        ):
            with self.subTest(body=body, is_encoded=is_encoded):
                response = api_handler(
                    {**route, "body": body, "isBase64Encoded": is_encoded}, None
                )
                self.assertEqual(response["statusCode"], status)
                expected = "missing_code" if body in ('{"party_size": 2}', encoded) else "invalid_json"
                self.assertEqual(json.loads(response["body"])["error"], expected)

    def test_empty_worker_batch_is_a_noop(self):
        self.assertIsNone(worker_handler({"Records": []}, None))

    def test_api_zip_serves_only_expected_get_assets(self):
        probe = """
import sys
sys.path.insert(0, sys.argv[1])
from api.handler import lambda_handler

def invoke(path, method="GET"):
    return lambda_handler({
        "routeKey": "$default", "rawPath": path,
        "requestContext": {"http": {"method": method}},
    }, None)

for path, mime, sample in (
    ("/", "text/html", 'id="event-form"'),
    ("/app.css", "text/css", ".seat"),
    ("/app.js", "text/javascript", "new URL(path.slice(1), apiBase)"),
):
    response = invoke(path)
    assert response["statusCode"] == 200
    assert response["headers"]["content-type"].startswith(mime)
    assert sample in response["body"]
rest_page = lambda_handler({
    "httpMethod": "GET", "resource": "/", "path": "/",
    "requestContext": {"stage": "prod"},
}, None)
assert rest_page["statusCode"] == 200
assert 'href="/prod/app.css"' in rest_page["body"]
assert 'src="/prod/app.js"' in rest_page["body"]
assert 'id="event-workspace" hidden' in rest_page["body"]
assert 'id="open-scanner"' in rest_page["body"]
assert 'Luna Vale Concert' not in rest_page["body"]
assert invoke("/", "POST")["statusCode"] == 404
assert invoke("/config/ticketing.json")["statusCode"] == 404
assert invoke("/.secrets/ticketing-ticket-secret")["statusCode"] == 404
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = build("api", Path(temp_dir))
            subprocess.run(
                [sys.executable, "-I", "-B", "-c", probe, str(archive_path)],
                cwd=temp_dir, check=True, timeout=10,
            )

    def test_zip_layout_and_handler_import_in_isolation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for target in ("api", "worker"):
                with self.subTest(target=target):
                    archive_path = build(target, Path(temp_dir))
                    with ZipFile(archive_path) as archive:
                        self.assertTrue({
                            f"{target}/__init__.py", f"{target}/handler.py",
                            "common/__init__.py", "common/db.py",
                        }.issubset(archive.namelist()))
                        self.assertEqual(
                            {name.split("/")[0] for name in archive.namelist()},
                            {target, "common"},
                        )
                    subprocess.run(
                        [
                            sys.executable, "-I", "-B", "-c",
                            "import importlib, sys; sys.path.insert(0, sys.argv[1]); "
                            "handler = importlib.import_module(sys.argv[2]); "
                            "assert callable(handler.lambda_handler)",
                            str(archive_path), f"{target}.handler",
                        ],
                        cwd=temp_dir, check=True, timeout=10,
                    )
