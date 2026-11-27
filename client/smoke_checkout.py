"""Verify the deployed hold, async checkout, and order status path."""

import argparse
import json
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from http_api import request

ROOT = Path(__file__).resolve().parents[1]


def purchase(base_url, event, timeout):
    base_url = base_url.rstrip("/")
    event_url = f"{base_url}/events/{event}"
    buyer, key = f"smoke-{uuid4().hex}", uuid4().hex
    status, hold = request(f"{event_url}/holds", {"party_size": 2}, buyer)
    if status != 201:
        raise SystemExit(f"Hold failed: HTTP {status} {hold}")
    checkout_url = f"{event_url}/holds/{hold['hold_id']}/checkout"
    for _ in range(2):
        status, submitted = request(checkout_url, {}, buyer, key)
        if status != 202 or submitted.get("order_id") != key:
            raise SystemExit(f"Checkout failed: HTTP {status}, error={submitted.get('error')}")

    deadline = monotonic() + timeout
    while monotonic() < deadline:
        status, order = request(f"{base_url}/orders/{key}")
        if status != 200:
            raise SystemExit(f"Order polling failed: HTTP {status}, error={order.get('error')}")
        if order["status"] == "failed":
            raise SystemExit(f"Order failed: {order['fail_reason']}")
        if order["status"] == "confirmed":
            break
        sleep(1)
    else:
        raise SystemExit("Checkout timed out while pending. Check the worker logs and DLQ.")

    expected_ids = set(hold["seats"])
    tickets = order["tickets"]
    if (
        not order["paid"] or len(tickets) != len(expected_ids) or
        {ticket["seat_id"] for ticket in tickets} != expected_ids or
        any(not ticket.get("code") for ticket in tickets)
    ):
        raise SystemExit("The confirmed order did not return the expected tickets.")
    status, inventory = request(f"{event_url}/seats")
    sold = {seat["seat_id"] for seat in inventory.get("seats", []) if seat["status"] == "sold"}
    if status != 200 or not expected_ids <= sold:
        raise SystemExit("The confirmed order's seats were not reported as sold.")
    status, replay = request(checkout_url, {}, buyer, key)
    if status != 202 or replay.get("order_id") != key or replay.get("status") != "confirmed":
        raise SystemExit("Checkout replay failed to return the confirmed order.")
    return tickets


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default="concert-1")
    parser.add_argument("--prefix", default="ticketing")
    parser.add_argument("--timeout", type=float, default=85)
    args = parser.parse_args()
    config = json.loads((ROOT / "config" / f"{args.prefix}.json").read_text(encoding="utf-8"))
    purchase(config["api_url"], args.event, args.timeout)
    print("Verified Phase 5: one confirmed order, two tickets, two sold seats, same order on replay.")
