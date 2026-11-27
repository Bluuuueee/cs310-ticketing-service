"""Retry a failed confirmation email for the newest matching confirmed order."""

import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
from uuid import uuid4

from package import ROOT, build
from setup import aws

sys.path.insert(0, str(ROOT / "server"))
from common.receipt_config import valid_email


def newest_failed_order(args):
    result = aws(args, "dynamodb", "scan",
                 "--table-name", f"{args.prefix}-orders", "--consistent-read",
                 "--filter-expression", "event_id = :event AND #st = :confirmed AND receipt_status = :failed AND receipt_email = :recipient",
                 "--expression-attribute-names", json.dumps({"#st": "status"}),
                 "--expression-attribute-values", json.dumps({
                     ":event": {"S": args.event}, ":confirmed": {"S": "confirmed"},
                     ":failed": {"S": "failed"}, ":recipient": {"S": args.recipient},
                 }),
                 "--projection-expression", "order_id, created_at_ms")
    items = result["Items"]
    return max(items, key=lambda item: int(item["created_at_ms"]["N"]))["order_id"]["S"] if items else None


def retry(args):
    identity = aws(args, "sts", "get-caller-identity")
    if identity["Account"] != args.account:
        raise SystemExit("AWS profile is authenticated to a different account.")
    order_id = newest_failed_order(args)
    if not order_id:
        raise SystemExit("No confirmed order with a failed receipt matched this event and email.")

    print(f"Selected the newest failed receipt for {args.event}, order ending {order_id[-8:]}.")
    print("This will update the Worker and attempt a confirmation email for that existing order.")
    if input("Type RETRY to continue: ") != "RETRY":
        raise SystemExit("Cancelled.")
    function = f"{args.prefix}-worker"
    archive = build("worker", ROOT / "dist")
    aws(args, "lambda", "update-function-code", "--function-name", function,
        "--zip-file", f"fileb://{archive}", "--query", "FunctionName")
    aws(args, "lambda", "wait", "function-updated-v2", "--function-name", function)

    with tempfile.TemporaryDirectory() as temp:
        request = Path(temp) / "request.json"
        result = Path(temp) / "result.json"
        request.write_text(json.dumps({"receipt_retry": {
            "order_id": order_id, "token": uuid4().hex,
        }}), encoding="ascii")
        metadata = aws(args, "lambda", "invoke", "--function-name", function,
                       "--payload", f"fileb://{request}", str(result))
        if metadata.get("FunctionError"):
            raise SystemExit("Worker invocation failed. Check CloudWatch Logs before retrying.")
        response = json.loads(result.read_text(encoding="utf-8"))

    print(f"Receipt status: {response['receipt']}")
    if response.get("error"):
        print(f"SES error code: {response['error']}")
    if response["receipt"] != "sent":
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--profile", default="cs310")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--prefix", default="ticketing")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{12}", args.account):
        parser.error("account must be a 12-digit AWS Account ID")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.prefix):
        parser.error("invalid resource prefix")
    if not re.fullmatch(r"[a-z0-9-]+", args.event) or not valid_email(args.recipient):
        parser.error("enter a valid event ID and the configured recipient address")
    retry(args)


if __name__ == "__main__":
    main()
