"""Preview or atomically delete the records associated with one demo event."""

import argparse
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import sys

from setup import ROOT, aws

sys.path.insert(0, str(ROOT / "server"))
from common.db import now_ms


def dynamodb(args, directory, operation, request):
    source = directory / "delete-event-request.json"
    source.write_text(json.dumps(request), encoding="utf-8")
    return aws(args, "dynamodb", operation, "--cli-input-json", f"file://{source}") or {}


def read_event(args, directory):
    prefix, event = args.prefix, args.event
    key = {"event_id": {"S": event}}
    names = {"#st": "status"}
    values = {":event": {"S": event}}
    metadata = dynamodb(args, directory, "get-item", {
        "TableName": f"{prefix}-events", "Key": key, "ConsistentRead": True,
        "ProjectionExpression": "event_id, created_at_ms, #name",
        "ExpressionAttributeNames": {"#name": "name"},
    }).get("Item")
    seats = dynamodb(args, directory, "query", {
        "TableName": f"{prefix}-seats", "ConsistentRead": True,
        "KeyConditionExpression": "event_id = :event",
        "ExpressionAttributeValues": values, "ExpressionAttributeNames": names,
        "ProjectionExpression": "seat_id, #st, hold_expires_at_ms",
    })["Items"]
    orders = dynamodb(args, directory, "scan", {
        "TableName": f"{prefix}-orders", "ConsistentRead": True,
        "FilterExpression": "event_id = :event", "ExpressionAttributeValues": values,
        "ExpressionAttributeNames": names, "ProjectionExpression": "order_id, #st, receipt_status",
    })["Items"]
    return (
        metadata,
        sorted(seats, key=lambda seat: seat["seat_id"]["S"]),
        sorted(orders, key=lambda order: order["order_id"]["S"]),
    )


def ensure_quiet(seats, orders, now):
    if any(order["status"]["S"] not in {"confirmed", "failed"} for order in orders):
        raise SystemExit("This event has unfinished orders. Wait for checkout to finish.")
    if any(order["status"]["S"] == "confirmed" and
           order.get("receipt_status", {}).get("S") in {"pending", "sending"} for order in orders):
        raise SystemExit("This event has unfinished receipt sends. Check the Worker before deleting it.")
    if any(seat["status"]["S"] == "held" and
           int(seat.get("hold_expires_at_ms", {}).get("N", now + 1)) > now for seat in seats):
        raise SystemExit("This event still has active seat holds. Let them expire.")


def delete_actions(args, item, seats, orders, now):
    prefix = args.prefix
    event = {"S": args.event}
    table = f"{prefix}-events"
    actions = []
    for order in orders:
        actions.append({"Delete": {
            "TableName": f"{prefix}-orders", "Key": {"order_id": order["order_id"]},
            "ConditionExpression": (
                "event_id = :event AND (#st = :confirmed OR #st = :failed) AND "
                "(#st = :failed OR attribute_not_exists(receipt_status) OR "
                "(receipt_status <> :pending AND receipt_status <> :sending))"
            ),
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": {
                ":event": event, ":confirmed": {"S": "confirmed"}, ":failed": {"S": "failed"},
                ":pending": {"S": "pending"}, ":sending": {"S": "sending"},
            },
        }})
    for seat in seats:
        actions.append({"Delete": {
            "TableName": f"{prefix}-seats", "Key": {"event_id": event, "seat_id": seat["seat_id"]},
            "ConditionExpression": "attribute_exists(seat_id) AND (#st <> :held OR hold_expires_at_ms <= :now)",
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": {":held": {"S": "held"}, ":now": {"N": str(now)}},
        }})

    key = {"event_id": event}
    if item is None:
        actions.append({"ConditionCheck": {
            "TableName": table, "Key": key, "ConditionExpression": "attribute_not_exists(event_id)",
        }})
    else:
        actions.append({"Delete": {
            "TableName": table, "Key": key, "ConditionExpression": "created_at_ms = :created",
            "ExpressionAttributeValues": {":created": item["created_at_ms"]},
        }})
    return actions


def remove_event(args):
    identity = aws(args, "sts", "get-caller-identity")
    if identity["Account"] != args.account:
        raise SystemExit("AWS profile is authenticated to a different account.")

    with TemporaryDirectory() as scratch:
        directory = Path(scratch)
        snapshot = read_event(args, directory)
        item, seats, orders = snapshot
        if not (item or seats or orders):
            print(f"No records found for {args.event}.")
            return
        name = item.get("name", {}).get("S") if item else None
        print(f"Account: {args.account} | Region: {args.region} | Event: {args.event}")
        if name:
            print(f"Concert: {name}")
        print(f"Matched {int(item is not None)} event, {len(seats)} seats and {len(orders)} orders.")
        ensure_quiet(seats, orders, now_ms())
        if len(seats) + len(orders) + 1 > 100:
            raise SystemExit("This event exceeds the single-transaction limit. No records were deleted.")
        if not args.execute:
            print("Preview only. Add --execute to confirm deletion.")
            return
        print("Keep this event out of use until the cleanup finishes.")
        if input(f"Type DELETE {args.event} to permanently remove its records: ") != f"DELETE {args.event}":
            raise SystemExit("Cancelled. No records were deleted.")
        if read_event(args, directory) != snapshot:
            raise SystemExit("The event changed during confirmation. No records were deleted.")
        now = now_ms()
        ensure_quiet(seats, orders, now)
        actions = delete_actions(args, item, seats, orders, now)
        dynamodb(args, directory, "transact-write-items", {"TransactItems": actions})
        remaining = read_event(args, directory)
        if any(remaining):
            raise SystemExit("Deletion ran, but read-back found remaining records. Check for concurrent requests.")
        print(f"Deleted and verified: {args.event}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="Expected 12-digit AWS account ID")
    parser.add_argument("--event", required=True, help="Exact event ID to match")
    parser.add_argument("--profile", default="cs310")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--prefix", default="ticketing")
    parser.add_argument("--execute", action="store_true", help="Prompt before permanent deletion")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{12}", args.account):
        parser.error("account must be a 12-digit AWS account ID")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.prefix):
        parser.error("invalid resource prefix")
    if not re.fullmatch(r"[a-z0-9-]+", args.event):
        parser.error("invalid event ID")
    remove_event(args)


if __name__ == "__main__":
    main()
