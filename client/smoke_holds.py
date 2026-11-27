"""Verify Phase 4 on a newly seeded correct-mode demo event."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

from http_api import request

ROOT = Path(__file__).resolve().parents[1]


def verify(base_url, event):
    event_url = f"{base_url.rstrip('/')}/events/{event}"
    status, seats = request(f"{event_url}/seats")
    if status != 200 or seats["mode"] != "correct" or len(seats["seats"]) != 50:
        raise SystemExit(f"Expected a correct-mode event with 50 seats, got HTTP {status}: {seats}")
    if any(seat["status"] != "available" for seat in seats["seats"]):
        raise SystemExit("Use a fresh event or wait until existing holds expire.")

    status, hold = request(f"{event_url}/holds", {"party_size": 2}, "best-seats")
    if status != 201 or hold["seats"] != ["A-5", "A-6"]:
        raise SystemExit(f"Expected A-5/A-6, got HTTP {status}: {hold}")

    barrier = Barrier(2)

    def compete(buyer, seats):
        barrier.wait(timeout=5)
        return request(f"{event_url}/holds", {"seats": seats}, buyer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(compete, "contest-1", ["B-4", "B-5"])
        second = pool.submit(compete, "contest-2", ["B-5", "B-6"])
        results = [first.result(), second.result()]
    if sorted(status for status, _ in results) != [201, 409]:
        raise SystemExit(f"Expected one success and one conflict: {results}")
    winner = next(body for status, body in results if status == 201)
    loser = next(body for status, body in results if status == 409)
    if loser.get("error") != "seats_taken":
        raise SystemExit(f"Unexpected conflict response: {loser}")

    status, seats = request(f"{event_url}/seats")
    held = {seat["seat_id"] for seat in seats.get("seats", []) if seat["status"] == "held"}
    if status != 200 or held != {"A-5", "A-6", *winner["seats"]}:
        raise SystemExit(f"Expected only four seats held by successful requests: {sorted(held)}")
    status, stats = request(f"{event_url}/stats")
    if status != 200 or (stats["held"], stats["available"], stats["sold"]) != (4, 46, 0):
        raise SystemExit(f"Unexpected stats (holds expire in 30 seconds): HTTP {status} {stats}")
    print("Verified Phase 4: A-5/A-6, one overlapping hold won, four seats held.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default="concert-1")
    parser.add_argument("--prefix", default="ticketing")
    args = parser.parse_args()
    config = json.loads((ROOT / "config" / f"{args.prefix}.json").read_text(encoding="utf-8"))
    verify(config["api_url"], args.event)
