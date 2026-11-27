"""Initialize and verify one CS310 demo event in DynamoDB."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from setup import ROOT, aws

sys.path.insert(0, str(ROOT / "server"))
from common.db import now_ms
from common.event_details import DEMO_EVENT


def local_timestamp(value, zone):
    if "T" not in value and " " not in value:
        raise ValueError("Include both a date and a time, such as 2026-12-19 20:00.")
    parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    if parsed.tzinfo is not None:
        return parsed.astimezone(zone)

    candidates = {}
    for fold in (0, 1):
        candidate = parsed.replace(tzinfo=zone, fold=fold)
        instant = candidate.astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None) == parsed:
            candidates[instant] = candidate
    if not candidates:
        raise ValueError(f"{value} does not exist in {zone.key} during the daylight saving transition.")
    if len(candidates) > 1:
        raise ValueError(f"{value} is ambiguous in {zone.key}; include the UTC offset.")
    return next(iter(candidates.values()))


def schedule(args):
    details = DEMO_EVENT.copy()
    if args.name is not None:
        name = args.name.strip()
        if not 1 <= len(name) <= 120 or not name.isprintable():
            raise ValueError("Event name must be 1 to 120 printable characters.")
        details["name"] = name
    if args.start is None and args.end is None and args.timezone is None:
        return details
    if not args.start or not args.end:
        raise ValueError("Supply both --start and --end when setting the show schedule.")

    zone_name = args.timezone or details["timezone"]
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Unknown or unavailable IANA time zone {zone_name!r}. "
            "Check the name, or install time zone data with python -m pip install tzdata."
        ) from exc
    start, end = local_timestamp(args.start, zone), local_timestamp(args.end, zone)
    if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
        raise ValueError("Show end time must be after the start time.")
    details.update(starts_at=start.isoformat(), ends_at=end.isoformat(), timezone=zone_name)
    return details


def records(prefix, event, mode, created_at, details):
    event_item = {
        "event_id": {"S": event},
        "rows": {"N": "5"},
        "seats_per_row": {"N": "10"},
        "capacity": {"N": "50"},
        "hold_seconds": {"N": "30"},
        "mode": {"S": mode},
        "conflicts": {"N": "0"},
        "created_at_ms": {"N": str(created_at)},
        **{key: {"S": value} for key, value in details.items()},
    }
    yield f"{prefix}-events", ("event_id",), event_item
    for row_index, row in enumerate("ABCDE"):
        for number in range(1, 11):
            yield f"{prefix}-seats", ("event_id", "seat_id"), {
                "event_id": {"S": event},
                "seat_id": {"S": f"{row}-{number}"},
                "row": {"S": row},
                "row_index": {"N": str(row_index)},
                "seat_num": {"N": str(number)},
                "status": {"S": "available"},
            }


def seed(args, details):
    identity = aws(args, "sts", "get-caller-identity")
    if identity["Account"] != args.account:
        raise SystemExit("AWS profile is authenticated to a different account.")

    expected = list(records(args.prefix, args.event, args.mode, now_ms(), details))
    writes = [
        {"Put": {"TableName": table, "Item": item,
                 "ConditionExpression": "attribute_not_exists(event_id)"}}
        for table, _, item in expected
    ]
    reads = [
        {"Get": {"TableName": table, "Key": {key: item[key] for key in keys}}}
        for table, keys, item in expected
    ]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "transaction.json"
        if not args.verify_only:
            path.write_text(json.dumps(writes), encoding="utf-8")
            print(f"Writing {len(expected)} items atomically for {args.event}", flush=True)
            aws(args, "dynamodb", "transact-write-items", "--transact-items", f"file://{path}")
        path.write_text(json.dumps(reads), encoding="utf-8")
        result = aws(args, "dynamodb", "transact-get-items", "--transact-items", f"file://{path}")

    actual = [response.get("Item", {}) for response in result["Responses"]]
    wanted = [item for _, _, item in expected]
    if args.verify_only and actual:
        if "created_at_ms" in actual[0]:
            wanted[0]["created_at_ms"] = actual[0]["created_at_ms"]
        ignored = []
        if args.name is None:
            ignored.append("name")
        if args.start is None:
            ignored.extend(("starts_at", "ends_at", "timezone"))
        for field in ignored:
            actual[0].pop(field, None)
            wanted[0].pop(field, None)
    if actual != wanted:
        raise SystemExit("Read-back mismatch. Verify the event before trying another write.")
    print(f"Verified {args.event} ({args.mode}): one event and 50 available seats.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="Expected 12-digit AWS Account ID for cloud operations")
    parser.add_argument("--profile", default="cs310")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--prefix", default="ticketing")
    parser.add_argument("--event", default="concert-1")
    parser.add_argument("--mode", choices=("correct", "naive"), default="correct")
    parser.add_argument("--name", help="Concert display name")
    parser.add_argument("--start", help="Local start date and time, e.g. 2026-12-19 20:00")
    parser.add_argument("--end", help="Local end date and time, e.g. 2026-12-19 22:00")
    parser.add_argument("--timezone", help="IANA time zone for the schedule, default America/Chicago")
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--dry-run", action="store_true", help="Describe writes offline")
    options.add_argument("--verify-only", action="store_true", help="Check untouched seed data")
    args = parser.parse_args()
    if not args.dry_run and not re.fullmatch(r"[0-9]{12}", args.account or ""):
        parser.error("account must be a 12-digit AWS Account ID")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.prefix):
        parser.error("invalid resource prefix")
    if not re.fullmatch(r"[a-z0-9-]+", args.event):
        parser.error("invalid event ID")
    try:
        details = schedule(args)
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))
    if args.dry_run:
        count = sum(1 for _ in records(args.prefix, args.event, args.mode, now_ms(), details))
        print(f"Would write {count} items for {args.event} in {args.region}.")
        print(f"{details['name']}: {details['starts_at']} to {details['ends_at']} ({details['timezone']})")
        return
    seed(args, details)


if __name__ == "__main__":
    main()
