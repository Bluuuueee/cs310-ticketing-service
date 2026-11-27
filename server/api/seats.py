from api.responses import error, response
from common.db import database, now_ms
from common.event_details import event_details
from common.receipt_config import test_recipient


def free(seat, now):
    return seat["status"] == "available" or (
        seat["status"] == "held" and seat["hold_expires_at_ms"] <= now
    )


def by_position(seat):
    return seat["row_index"], seat["seat_num"]


def list_seats(params, headers, body):
    event_id = params["event_id"]
    db = database()
    event = db.get_event(event_id)
    if event is None:
        return error(404, "event_not_found", "Event not found")

    seats = sorted(db.list_seats(event_id), key=by_position)
    now = now_ms()
    displayed = []
    for seat in seats:
        available = free(seat, now)
        displayed.append({
            "seat_id": seat["seat_id"],
            "row": seat["row"],
            "seat_num": int(seat["seat_num"]),
            "status": "available" if available else seat["status"],
            "holder": None if available else seat.get("holder"),
        })
    return response(200, {
        "event_id": event_id, "rows": int(event["rows"]),
        "seats_per_row": int(event["seats_per_row"]), "mode": event["mode"],
        "server_time_ms": now, "seats": displayed,
        "event": event_details(event), "confirmation_email_enabled": bool(test_recipient()),
    })
