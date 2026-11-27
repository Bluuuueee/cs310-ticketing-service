from collections import Counter

from api.responses import error, response
from api.seats import free
from common.db import database, now_ms


def stats(params, headers, body):
    event_id = params["event_id"]
    db = database()
    event = db.get_event(event_id)
    if event is None:
        return error(404, "event_not_found", "Event not found")

    seats = db.list_seats(event_id)
    now = now_ms()
    inventory = Counter("available" if free(seat, now) else seat["status"] for seat in seats)
    orders = Counter()
    confirmed_seats = Counter()
    for order in db.list_orders(event_id):
        orders[order["status"]] += 1
        if order["status"] == "confirmed":
            confirmed_seats.update(order["seats"])
    return response(200, {
        "event_id": event_id,
        "capacity": int(event["capacity"]),
        **{status: inventory[status] for status in ("available", "held", "sold")},
        "tickets_issued": sum(confirmed_seats.values()),
        "double_booked_seats": sorted(
            (seat for seat, count in confirmed_seats.items() if count > 1),
            key=lambda seat: (seat.split("-")[0], int(seat.split("-")[1])),
        ),
        "conflicts": int(event["conflicts"]),
        "orders": {status: orders[status] for status in ("pending", "confirmed", "failed")},
    })
