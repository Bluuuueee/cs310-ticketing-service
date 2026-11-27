"""Attach a prod REST API to the existing ticketing API Lambda."""

import argparse
import json
from pathlib import Path
import re
import tempfile
from urllib.request import urlopen

from package import ROOT, build
from rest_gateway import rest_gateway
from setup import aws, verify_api


def verify_web(api_url):
    for route, mime, marker in (
        ("/", "text/html", b'id="event-form"'),
        ("/app.css", "text/css", b".seat"),
        ("/app.js", "text/javascript", b"apiBase"),
    ):
        with urlopen(api_url + route, timeout=15) as response:
            if response.status != 200 or response.headers.get_content_type() != mime:
                raise SystemExit(f"Unexpected REST response for {route}")
            if marker not in response.read():
                raise SystemExit(f"Expected demo content at {route}")

    with urlopen(api_url + "/events?name=deployment-probe", timeout=15) as response:
        if response.status != 200 or not isinstance(json.load(response).get("events"), list):
            raise SystemExit("Unexpected REST response for event search.")


def attach(args):
    identity = aws(args, "sts", "get-caller-identity")
    if identity["Account"] != args.account:
        raise SystemExit("AWS profile is authenticated to a different account.")

    config_path = ROOT / "config" / f"{args.prefix}.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read existing deployment configuration: {exc}")
    if config.get("region") != args.region or config.get("prefix") != args.prefix:
        raise SystemExit("Region or prefix does not match the existing deployment configuration.")
    if config.get("gateway_type") == "rest" and not config.get("rest_stack_name"):
        verify_api(config["api_url"])
        verify_web(config["api_url"])
        print(f"Existing REST API already verified: {config['api_url']}/")
        return

    function = f"{args.prefix}-api"
    function_arn = aws(args, "lambda", "get-function", "--function-name", function,
                       "--query", "Configuration.FunctionArn")
    if not function_arn:
        raise SystemExit("Existing API Lambda was not found.")
    archive = build("api", ROOT / "dist")
    print(f"Uploading REST-compatible code to {function}", flush=True)
    aws(args, "lambda", "update-function-code", "--function-name", function,
        "--zip-file", f"fileb://{archive}", "--query", "FunctionName")
    aws(args, "lambda", "wait", "function-updated-v2", "--function-name", function)

    stack = f"{args.prefix}-rest-gateway"
    resources, url = rest_gateway(args.prefix, function, function_arn)
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": resources, "Outputs": {"ApiUrl": {"Value": url}},
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "rest-gateway.json"
        path.write_text(json.dumps(template), encoding="utf-8")
        aws(args, "cloudformation", "validate-template", "--template-body", f"file://{path}")
        print(f"Deploying CloudFormation stack {stack} in {args.region}", flush=True)
        aws(args, "cloudformation", "deploy", "--stack-name", stack, "--template-file", str(path),
            "--no-fail-on-empty-changeset", stream=True)

    stack_data = aws(args, "cloudformation", "describe-stacks", "--stack-name", stack)
    outputs = {row["OutputKey"]: row["OutputValue"] for row in stack_data["Stacks"][0]["Outputs"]}
    rest_url = outputs["ApiUrl"].rstrip("/")
    verify_api(rest_url)
    verify_web(rest_url)

    if config.get("gateway_type") != "rest":
        config["previous_http_api_url"] = config["api_url"]
    config.update(api_url=rest_url, gateway_type="rest", rest_stack_name=stack)
    temporary = config_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temporary.replace(config_path)
    print(f"Verified REST API and demo page over HTTPS: {rest_url}/")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="Expected 12-digit AWS Account ID")
    parser.add_argument("--profile", default="cs310")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--prefix", default="ticketing")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{12}", args.account):
        parser.error("account must be a 12-digit AWS Account ID")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.prefix):
        parser.error("invalid resource prefix")
    attach(args)


if __name__ == "__main__":
    main()
