"""Send a single sandbox email attempt after an order is confirmed."""

from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from functools import lru_cache
import logging
import os
import re

from common.event_details import event_details
from common.receipt_config import test_recipient

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def ses_client():
    import boto3
    from botocore.config import Config

    return boto3.client("sesv2", config=Config(
        connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1},
    ))


def calendar_text(value):
    return (value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n")
            .replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,"))


def fold_calendar_line(line):
    parts = []
    chunk = bytearray()
    for char in line:
        encoded = char.encode("utf-8")
        if len(chunk) + len(encoded) > 75:
            parts.append(bytes(chunk))
            chunk = bytearray(b" ")
        chunk.extend(encoded)
    parts.append(bytes(chunk))
    return b"\r\n".join(parts)


def utc_calendar_time(value):
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def calendar_attachment(event_id, details, stamped_at):
    start = datetime.fromisoformat(details["starts_at"])
    end = datetime.fromisoformat(details["ends_at"])
    if start.utcoffset() is None or end.utcoffset() is None or end <= start:
        raise ValueError("Calendar dates must include offsets and end after the start.")
    return b"\r\n".join(map(fold_calendar_line, [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CS310//Ticketing Demo//EN",
        "BEGIN:VEVENT", f"UID:{calendar_text(event_id)}@ticketing-demo.invalid",
        f"DTSTAMP:{utc_calendar_time(stamped_at)}",
        f"DTSTART:{utc_calendar_time(start)}",
        f"DTEND:{utc_calendar_time(end)}",
        f"SUMMARY:{calendar_text(details['name'])}",
        "END:VEVENT", "END:VCALENDAR", "",
    ]))


def build_receipt(sender, recipient, order, event):
    details = event_details(event)
    message = EmailMessage(policy=SMTP)
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"Booking confirmed: {details['name']}"
    codes = "\n".join(f"{ticket['seat_id']}: {ticket['code']}" for ticket in order.get("tickets", []))
    ticket_section = f"Ticket codes (keep these private):\n{codes}\n\n" if codes else ""
    message.set_content(
        f"Your demo order for {details['name']} is confirmed.\n\n"
        f"Order: {order['order_id']}\n"
        f"Seats: {', '.join(order['seats'])}\n"
        f"Start: {details['starts_at']}\n"
        f"End: {details['ends_at']}\n"
        f"Time zone: {details['timezone']}\n\n"
        f"{ticket_section}"
        "To add the concert to Google Calendar, download the attached event.ics "
        "and import it using Settings > Import & export.\n"
    )
    stamp = datetime.fromtimestamp(int(order["updated_at_ms"]) / 1000, timezone.utc)
    invite = calendar_attachment(order["event_id"], details, stamp)
    message.add_attachment(invite, maintype="text", subtype="calendar", filename="event.ics",
                           params={"charset": "utf-8"})
    return message.as_bytes()


def send_receipt(db, order, event, retry_token=None):
    sender = os.environ.get("SES_FROM_EMAIL", "")
    recipient = order.get("receipt_email")
    if not sender or not recipient or recipient != test_recipient():
        return
    order_id = order["order_id"]
    if not db.claim_receipt(order_id, recipient, retry_token=retry_token):
        return

    status = "failed"
    error_code = None
    try:
        message = build_receipt(sender, recipient, order, event)
        ses_client().send_email(
            FromEmailAddress=sender, Destination={"ToAddresses": [recipient]},
            Content={"Raw": {"Data": message}},
        )
        status = "sent"
    except Exception as exc:
        # Avoid logging addresses or automatically resending an ambiguous SES request.
        service_error = getattr(exc, "response", {}).get("Error", {})
        error_code = service_error.get("Code")
        if not isinstance(error_code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", error_code):
            error_code = type(exc).__name__
        if error_code in ("AccessDenied", "AccessDeniedException"):
            action = re.search(r"\bses:Send(?:Raw)?Email\b", str(service_error.get("Message", "")))
            if action:
                error_code = f"{error_code} ({action.group()})"
        logger.error("Receipt submission unsuccessful (%s)", error_code)
    db.finish_receipt(order_id, status, error_code)
