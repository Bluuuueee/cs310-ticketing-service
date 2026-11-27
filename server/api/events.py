"""Search the small demo event catalog by its stored concert title."""

from api.responses import error, response
from common.db import database


def search_events(query):
    name = query.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        return error(400, "invalid_event_search", "Enter a concert name or event ID.")
    term = name.strip().casefold()

    matches = []
    for event in database().list_events():
        event_id = event["event_id"]
        configured_name = event.get("name")
        title = configured_name if isinstance(configured_name, str) and configured_name else event_id
        if term not in title.casefold() and term != event_id.casefold():
            continue
        details = {
            key: event[key] for key in ("starts_at", "ends_at", "timezone")
            if isinstance(event.get(key), str)
        }
        matches.append({"event_id": event_id, "name": title, **details})

    matches.sort(key=lambda item: (
        item["name"].casefold() != term and item["event_id"].casefold() != term,
        item["name"].casefold(), item["event_id"],
    ))
    return response(200, {"events": matches[:25], "truncated": len(matches) > 25})
