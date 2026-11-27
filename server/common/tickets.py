import base64
import hmac
import re


def signature(secret, message):
    digest = hmac.digest(secret.encode("utf-8"), message.encode("utf-8"), "sha256")
    return base64.urlsafe_b64encode(digest[:16]).decode("ascii").rstrip("=")


def make_code(secret, event_id, seat_id, order_id):
    message = "|".join((event_id, seat_id, order_id))
    return f"{message}|{signature(secret, message)}"


def verify_code(secret, code):
    parts = code.split("|")
    if len(parts) != 4:
        return None
    event_id, seat_id, order_id, supplied = parts
    if (
        not re.fullmatch(r"[a-z0-9-]+", event_id) or
        not re.fullmatch(r"[A-E]-(?:[1-9]|10)", seat_id) or
        not re.fullmatch(r"[0-9a-fA-F]{32}", order_id) or
        not re.fullmatch(r"[A-Za-z0-9_-]{22}", supplied)
    ):
        return None
    if hmac.compare_digest(supplied, signature(secret, "|".join(parts[:3]))):
        return event_id, seat_id, order_id
    return None
