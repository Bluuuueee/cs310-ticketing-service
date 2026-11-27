import json
import os
import re
from functools import lru_cache

from api.responses import error, response
from api.seats import by_position
from common.db import database, now_ms
from common.receipt_config import test_recipient, valid_email


@lru_cache(maxsize=1)
def queue_client():
    import boto3

    return boto3.client("sqs")


def enqueue(order_id):
    queue_client().send_message(
        QueueUrl=os.environ["CHECKOUT_QUEUE_URL"],
        MessageBody=json.dumps({"order_id": order_id}),
    )


def checkout(params, headers, body):
    buyer = headers.get("x-buyer-id")
    if not buyer or "|" in buyer:
        return error(400, "missing_buyer_id", "A valid X-Buyer-Id is required")
    order_id = headers.get("idempotency-key", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", order_id):
        return error(400, "missing_idempotency_key", "A 32-character hexadecimal Idempotency-Key is required")

    email = body.get("email") if isinstance(body, dict) else None
    if email is not None:
        if not valid_email(email):
            return error(400, "invalid_email", "Enter a valid email address")
        allowed = test_recipient()
        if not allowed:
            return error(400, "email_unavailable", "Confirmation email is disabled")
        if allowed.lower() != email.lower():
            return error(400, "email_not_allowed", "Use the configured demo email address or leave it blank")

    event_id, hold_id = params["event_id"], params["hold_id"]
    db = database()
    matching = [seat for seat in db.list_seats(event_id) if seat.get("hold_id") == hold_id]
    if not matching:
        return error(404, "hold_not_found", "Hold not found")
    if any(seat["holder"] != buyer for seat in matching):
        return error(403, "not_your_hold", "This hold belongs to another buyer")

    now = now_ms()
    order = {
        "order_id": order_id, "event_id": event_id, "hold_id": hold_id,
        "buyer_id": buyer, "seats": [seat["seat_id"] for seat in sorted(matching, key=by_position)],
        "status": "pending", "paid": False, "created_at_ms": now, "updated_at_ms": now,
    }
    if email is not None:
        order.update(receipt_email=allowed, receipt_status="pending")
    if db.create_order(order):
        enqueue(order_id)
    else:
        order = db.get_order(order_id)
    return response(202, {"order_id": order_id, "status": order["status"]})
