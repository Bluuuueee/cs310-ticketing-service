"""Handle checkout messages delivered by the SQS mapping."""

import json
import os
import re
from random import random, uniform
from time import sleep

from common.db import database, now_ms
from common.tickets import make_code
from worker.receipts import send_receipt


def simulate_payment():
    sleep(uniform(1, 3))
    if random() < float(os.environ.get("PAYMENT_FAILURE_RATE", "0.1")):
        raise RuntimeError("Temporary simulated payment failure")


def process_order(order_id):
    db = database()
    order = db.get_order(order_id)
    if order is None or order["status"] == "failed":
        return

    if order["status"] == "confirmed":
        if order.get("receipt_status") == "pending":
            event = db.get_event(order["event_id"])
            if event is not None:
                send_receipt(db, order, event)
        return

    event = db.get_event(order["event_id"])
    simulate_payment()
    secret = os.environ["TICKET_SECRET"]
    tickets = [
        {"seat_id": seat, "code": make_code(secret, order["event_id"], seat, order_id)}
        for seat in order["seats"]
    ]
    db.confirm_order(order, tickets, now_ms(), event["mode"] == "naive")
    if order.get("receipt_email"):
        latest = db.get_order(order_id)
        if latest is not None and latest["status"] == "confirmed":
            send_receipt(db, latest, event)


def retry_receipt(request):
    order_id, token = request["order_id"], request["token"]
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{32}", value)
               for value in (order_id, token)):
        return {"receipt": "not_eligible"}
    db = database()
    order = db.get_order(order_id)
    if order is None or order["status"] != "confirmed" or order.get("receipt_status") != "failed":
        return {"receipt": "not_eligible"}
    event = db.get_event(order["event_id"])
    if event is None:
        return {"receipt": "not_eligible"}
    send_receipt(db, order, event, retry_token=token)
    current = db.get_order(order_id) or {}
    return {"receipt": current.get("receipt_status", "not_eligible"), "error": current.get("receipt_error")}


def lambda_handler(event, context):
    if "receipt_retry" in event:
        # This entry point is only available via IAM-authorized Worker invocation.
        return retry_receipt(event["receipt_retry"])
    for record in event["Records"]:
        process_order(json.loads(record["body"])["order_id"])
