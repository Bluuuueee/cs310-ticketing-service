from collections import defaultdict
import re
from uuid import uuid4

from api.responses import error, response
from api.seats import by_position, free
from common.db import SeatClaimConflict, database, now_ms


def best_block(seats, size, width, now):
    rows = defaultdict(list)
    for seat in sorted(seats, key=by_position):
        if free(seat, now):
            rows[seat["row_index"]].append(seat)

    best_key = None
    best_ids = []
    for row_index, row in rows.items():
        for start in range(len(row) - size + 1):
            block = row[start:start + size]
            left = block[0]["seat_num"]
            if block[-1]["seat_num"] - left != size - 1:
                continue
            # Scores scaled by four preserve exact ranking and ties.
            score = 4 * row_index * size + sum(
                abs(2 * seat["seat_num"] - (width + 1)) for seat in block
            )
            key = score, row_index, left
            if best_key is None or key < best_key:
                best_key = key
                best_ids = [seat["seat_id"] for seat in block]
    return best_ids


def hold(params, headers, body):
    buyer = headers.get("x-buyer-id")
    if not buyer or "|" in buyer:
        return error(400, "missing_buyer_id", "A valid X-Buyer-Id is required")
    if not isinstance(body, dict):
        return error(400, "invalid_party_size", "Supply a party_size or a seats list")

    explicit = "seats" in body
    if explicit:
        requested = body["seats"]
        if (
            "party_size" in body or not isinstance(requested, list) or
            not 1 <= len(requested) <= 4 or
            any(not isinstance(seat, str) or not re.fullmatch(r"[A-E]-(?:[1-9]|10)", seat)
                for seat in requested) or len(set(requested)) != len(requested)
        ):
            return error(400, "invalid_seats", "Supply 1 to 4 distinct seat IDs")
    else:
        size = body.get("party_size")
        if type(size) is not int or not 1 <= size <= 4:
            return error(400, "invalid_party_size", "party_size must be an integer from 1 to 4")

    event_id = params["event_id"]
    db = database()
    event = db.get_event(event_id)
    if event is None:
        return error(404, "event_not_found", "Event not found")
    naive = event["mode"] == "naive"
    hold_id = f"h-{uuid4().hex[:12]}"

    for _ in range(1 if explicit or naive else 3):
        seats = db.list_seats(event_id) if not explicit or naive else None
        now = now_ms()
        if explicit:
            selected = requested
            if naive and not set(selected) <= {
                seat["seat_id"] for seat in seats if free(seat, now)
            }:
                break
        else:
            selected = best_block(seats, size, event["seats_per_row"], now)
            if not selected:
                return error(409, "no_block_available", f"No block of {size} adjacent free seats")

        expires = now + int(event["hold_seconds"]) * 1000
        try:
            db.claim_seats(event_id, selected, buyer, hold_id, now, expires, naive)
        except SeatClaimConflict:
            if not explicit:
                db.record_conflict(event_id)
            continue
        return response(201, {
            "hold_id": hold_id, "event_id": event_id,
            "seats": selected, "expires_at_ms": expires,
        })
    return error(409, "seats_taken", "The requested seats could not be claimed")
