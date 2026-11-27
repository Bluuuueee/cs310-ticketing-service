"""Public schedule fields with sample values for previously seeded demo events."""

DEMO_EVENT = {
    "name": "Luna Vale Concert",
    "starts_at": "2026-12-12T20:00:00-06:00",
    "ends_at": "2026-12-12T22:00:00-06:00",
    "timezone": "America/Chicago",
}


def event_details(event):
    return {key: event.get(key, default) for key, default in DEMO_EVENT.items()}
