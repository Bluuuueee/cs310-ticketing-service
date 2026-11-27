"""Compare correct and naive ticketing on two freshly seeded demo events."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from time import monotonic, sleep
from urllib.error import URLError
from uuid import uuid4

from http_api import request

ROOT = Path(__file__).resolve().parents[1]


def measured_request(url, body=None, buyer=None, key=None):
    started = monotonic()
    try:
        status, data = request(url, body, buyer, key)
    except (URLError, OSError, ValueError) as exc:
        return 0, {"error": type(exc).__name__}, (monotonic() - started) * 1000
    return status, data, (monotonic() - started) * 1000


def require_get(url):
    status, data, _ = measured_request(url)
    if status != 200:
        raise SystemExit(f"Could not read {url.rsplit('/', 1)[-1]}: HTTP {status}, {data.get('error')}")
    return data


def check_fresh(base, event, mode):
    url = f"{base}/events/{event}"
    seats = require_get(url + "/seats")
    stats = require_get(url + "/stats")
    if seats["mode"] != mode:
        raise SystemExit(f"Event {event} must be in {mode} mode.")
    if (
        len(seats["seats"]) != stats["capacity"] or
        any(seat["status"] != "available" for seat in seats["seats"]) or
        sum(stats["orders"].values()) != 0 or stats["conflicts"] != 0
    ):
        raise SystemExit(f"Event {event} is not fresh. Seed a new event for this run.")


def percentile(samples, fraction):
    if not samples:
        return None
    ordered = sorted(samples)
    return round(ordered[math.ceil(len(ordered) * fraction) - 1], 1)


def observe_orders(pool, base, pending, timeout, errors):
    active = set(pending)
    counts = Counter()
    failure_reasons = Counter()
    deadline = monotonic() + timeout
    while active and monotonic() < deadline:
        ids = sorted(active)
        results = pool.map(lambda key: measured_request(f"{base}/orders/{key}"), ids)
        for key, (status, data, _) in zip(ids, results):
            if status != 200:
                errors[f"poll:{status}:{data.get('error', 'http_error')}"] += 1
                continue
            if data["status"] in {"confirmed", "failed"}:
                counts[data["status"]] += 1
                active.remove(key)
                if data["status"] == "failed":
                    failure_reasons[data.get("fail_reason") or "unspecified"] += 1
        if active and monotonic() < deadline:
            sleep(min(2, max(0, deadline - monotonic())))
    return {
        "confirmed": counts["confirmed"], "failed": counts["failed"],
        "unresolved": len(active), "failure_reasons": dict(failure_reasons),
    }


def compare_one(base, event, mode, args):
    started = monotonic()
    url = f"{base}/events/{event}"
    errors = Counter()
    latencies = {"hold": [], "checkout": []}
    accepted = []
    successes = 0

    def hold_for(buyer):
        return measured_request(url + "/holds", {"party_size": args.party_size}, buyer)

    def submit(buyer, hold_id, key):
        return measured_request(url + f"/holds/{hold_id}/checkout", {}, buyer, key)

    def load_buyer(buyer):
        hold = hold_for(buyer)
        if hold[0] != 201:
            return hold, None, None
        key = uuid4().hex
        return hold, submit(buyer, hold[1]["hold_id"], key), key

    def record_checkout(result, key):
        status, data, elapsed = result
        latencies["checkout"].append(elapsed)
        if status == 202 and data.get("order_id") == key:
            accepted.append(key)
        else:
            errors[f"checkout:{status}:{data.get('error', 'unexpected_response')}"] += 1

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        if args.scenario == "overlap":
            buyer = f"overlap-{uuid4().hex}"
            status, data, elapsed = hold_for(buyer)
            latencies["hold"].append(elapsed)
            if status != 201:
                errors[f"hold:{status}:{data.get('error', 'http_error')}"] += 1
            else:
                successes = 1
                attempts = [
                    (key, pool.submit(submit, buyer, data["hold_id"], key))
                    for key in (uuid4().hex, uuid4().hex)
                ]
                for key, future in attempts:
                    record_checkout(future.result(), key)
        else:
            buyers = [f"load-{uuid4().hex}" for _ in range(args.buyers)]
            for (status, data, elapsed), checkout, key in pool.map(load_buyer, buyers):
                latencies["hold"].append(elapsed)
                if status != 201:
                    errors[f"hold:{status}:{data.get('error', 'http_error')}"] += 1
                    continue
                successes += 1
                record_checkout(checkout, key)
        observed = observe_orders(pool, base, accepted, args.timeout, errors)

    snapshot = require_get(url + "/stats")
    return {
        "event": event, "mode": mode, "hold_attempts": len(latencies["hold"]),
        "successful_holds": successes, "checkout_attempts": len(latencies["checkout"]),
        "accepted_checkouts": len(accepted), "orders_observed": observed,
        "http_errors": dict(errors),
        "latency_ms": {
            stage: {"requests": len(samples), "p50": percentile(samples, .5), "p95": percentile(samples, .95)}
            for stage, samples in latencies.items()
        },
        "stats": snapshot, "elapsed_seconds": round(monotonic() - started, 2),
    }


def verdict(correct, naive, scenario):
    c, n = correct["stats"], naive["stats"]
    if c["double_booked_seats"] or c["tickets_issued"] > c["capacity"]:
        return "FAILED: correct-mode seat invariant violated."
    if any(result["orders_observed"]["unresolved"] or result["stats"]["orders"]["pending"]
           for result in (correct, naive)):
        return "INCONCLUSIVE: orders were not terminal at the observation deadline."
    if scenario == "overlap":
        if (
            correct["accepted_checkouts"] == naive["accepted_checkouts"] == 2 and
            c["orders"]["confirmed"] == 1 and c["orders"]["failed"] == 1 and
            n["orders"]["confirmed"] == 2 and n["double_booked_seats"]
        ):
            return "OBSERVED: the same hold sold twice in naive mode and once in correct mode."
        return "INCONCLUSIVE: the controlled checkout outcomes did not all occur."
    if not c["orders"]["confirmed"] or not n["orders"]["confirmed"]:
        return "INCONCLUSIVE: at least one mode did not complete a confirmed checkout."
    if n["double_booked_seats"]:
        return "OBSERVED: only naive mode reported double-booked seats under this workload."
    return "OBSERVED: neither mode reported double-booked seats under this workload."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("load", "overlap"), default="load")
    parser.add_argument("--correct-event", required=True)
    parser.add_argument("--naive-event", required=True)
    parser.add_argument("--prefix", default="ticketing")
    parser.add_argument("--buyers", type=int, default=10, help="Buyer count per event for load")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--party-size", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--timeout", type=int, default=85, help="Seconds to poll after submissions")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if not 1 <= args.buyers <= 100 or not 1 <= args.concurrency <= min(25, args.buyers):
        parser.error("use 1 to 100 buyers and concurrency up to 25 and no more than the buyer count")
    if args.scenario == "overlap" and args.concurrency < 2:
        parser.error("overlap needs concurrency of at least 2")
    if not 5 <= args.timeout <= 180:
        parser.error("timeout must be from 5 to 180 seconds")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.prefix):
        parser.error("invalid resource prefix")
    pairs = (("correct", args.correct_event), ("naive", args.naive_event))
    if args.correct_event == args.naive_event or not all(
        re.fullmatch(r"[a-z0-9-]+", event) for _, event in pairs
    ):
        parser.error("two different valid event IDs are required")
    if args.out.exists():
        parser.error("output already exists; choose a new path")

    config = json.loads((ROOT / "config" / f"{args.prefix}.json").read_text(encoding="utf-8"))
    base = config["api_url"].rstrip("/")
    for mode, event in pairs:
        check_fresh(base, event, mode)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "scenario": args.scenario, "party_size": args.party_size,
        "concurrency": args.concurrency if args.scenario == "load" else 2,
        "buyers_per_event": args.buyers if args.scenario == "load" else 1,
        "poll_timeout_seconds": args.timeout, "results": [],
    }
    with args.out.open("x", encoding="utf-8"):
        pass
    for mode, event in pairs:
        print(f"Running {args.scenario}: {mode} on {event}", flush=True)
        result = compare_one(base, event, mode, args)
        report["results"].append(result)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        stats = result["stats"]
        print(f"{mode}: {result['successful_holds']} holds, {stats['orders']}, "
              f"{stats['tickets_issued']} tickets, double booked {stats['double_booked_seats']}")
    report["verdict"] = verdict(*report["results"], args.scenario)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(report["verdict"])
    print(f"Saved report: {args.out}")
    if report["verdict"].startswith("FAILED"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
