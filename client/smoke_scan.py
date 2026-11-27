"""Verify first use, replay, invalid signature, and concurrent scans over HTTPS."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

from http_api import request
from smoke_checkout import purchase

ROOT = Path(__file__).resolve().parents[1]


def verify(base_url, event, timeout):
    first, second = purchase(base_url, event, timeout)
    url = f"{base_url.rstrip('/')}/tickets/scan"

    def scan_ticket(code, gate):
        status, body = request(url, {"code": code, "gate": gate})
        if status != 200:
            raise SystemExit(f"Scan failed: HTTP {status}, error={body.get('error')}")
        return body

    message, signature = first["code"].rsplit("|", 1)
    changed = "B" if signature[0] == "A" else "A"
    if scan_ticket(f"{message}|{changed}{signature[1:]}", "north") != {"result": "INVALID"}:
        raise SystemExit("Tampered ticket did not return INVALID.")
    if scan_ticket(first["code"], "north") != {"result": "ADMIT", "seat_id": first["seat_id"]}:
        raise SystemExit("The first valid scan did not admit the ticket.")
    repeated = scan_ticket(first["code"], "south")
    if (
        repeated.get("result") != "ALREADY_USED" or repeated.get("first_gate") != "north" or
        repeated.get("seat_id") != first["seat_id"] or
        type(repeated.get("first_used_at_ms")) is not int
    ):
        raise SystemExit("Replay did not return the original gate and usage time.")

    barrier = Barrier(2)

    def compete(gate):
        barrier.wait(timeout=5)
        return gate, scan_ticket(second["code"], gate)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ("north", "south")))
    outcomes = {body.get("result"): (gate, body) for gate, body in results}
    if outcomes.keys() != {"ADMIT", "ALREADY_USED"}:
        raise SystemExit("Concurrent scans should admit exactly one request.")
    winning_gate, admitted = outcomes["ADMIT"]
    _, rejected = outcomes["ALREADY_USED"]
    if (
        admitted.get("seat_id") != second["seat_id"] or
        rejected.get("seat_id") != second["seat_id"] or
        rejected.get("first_gate") != winning_gate or
        type(rejected.get("first_used_at_ms")) is not int
    ):
        raise SystemExit("Concurrent replay did not return the winning scan metadata.")
    print("Verified Phase 6: first scan admitted, replay rejected, tampering rejected, one concurrent admit.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default="concert-1")
    parser.add_argument("--prefix", default="ticketing")
    parser.add_argument("--timeout", type=float, default=85)
    args = parser.parse_args()
    config = json.loads((ROOT / "config" / f"{args.prefix}.json").read_text(encoding="utf-8"))
    verify(config["api_url"], args.event, args.timeout)
