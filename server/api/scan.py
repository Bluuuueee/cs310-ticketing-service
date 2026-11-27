import os

from api.responses import error, response
from common.db import database, now_ms
from common.tickets import verify_code


def scan(params, headers, body):
    code = body.get("code") if isinstance(body, dict) else None
    if not isinstance(code, str) or not code:
        return error(400, "missing_code", "A ticket code is required")
    verified = verify_code(os.environ["TICKET_SECRET"], code)
    if verified is None:
        return response(200, {"result": "INVALID"})

    event_id, seat_id, order_id = verified
    gate = body.get("gate", "")
    if not isinstance(gate, str):
        gate = ""

    db = database()
    if db.use_ticket(event_id, seat_id, order_id, gate, now_ms()):
        return response(200, {"result": "ADMIT", "seat_id": seat_id})

    seat = db.get_seat(event_id, seat_id) or {}
    first_used = seat.get("used_at_ms")
    return response(200, {
        "result": "ALREADY_USED", "seat_id": seat_id,
        "first_gate": seat.get("used_gate"),
        "first_used_at_ms": int(first_used) if first_used is not None else None,
    })
