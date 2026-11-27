"""API Gateway Lambda proxy entry point for REST API and HTTP API."""

import base64
import binascii
import json
import logging

from api.checkout import checkout
from api.events import search_events
from api.holds import hold
from api.orders import get_order
from api.responses import error
from api.scan import scan
from api.seats import list_seats
from api.stats import stats
from api.web_assets import web_response

logger = logging.getLogger(__name__)

ROUTES = {
    "POST /events/{event_id}/holds": hold,
    "GET /events/{event_id}/seats": list_seats,
    "GET /events/{event_id}/stats": stats,
    "POST /events/{event_id}/holds/{hold_id}/checkout": checkout,
    "GET /orders/{order_id}": get_order,
    "POST /tickets/scan": scan,
}


def lambda_handler(event: dict, context: object) -> dict:
    try:
        route = event.get("routeKey")
        request_context = event.get("requestContext") or {}
        if route == "$default":
            method = request_context.get("http", {}).get("method")
            path = event.get("rawPath")
            asset = web_response(method, path)
            if method == "GET" and path == "/events":
                route = "GET /events"
            if asset is not None:
                return asset
        elif route is None and "httpMethod" in event and "resource" in event:
            method, resource = event["httpMethod"], event["resource"]
            asset = web_response(method, resource, request_context.get("stage", ""))
            if asset is not None:
                return asset
            route = f"{method} {resource}"
        if route == "GET /events":
            return search_events(event.get("queryStringParameters") or {})
        if route not in ROUTES:
            return error(404, "not_found", "Unknown route")

        body = event.get("body")
        try:
            if body and event.get("isBase64Encoded"):
                body = base64.b64decode(body, validate=True).decode("utf-8")
            payload = json.loads(body) if body else {}
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
            return error(400, "invalid_json", "Request body must contain valid JSON")

        operation = ROUTES[route]
        params = event.get("pathParameters") or {}
        headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
        return operation(params, headers, payload)
    except Exception:
        logger.exception("Unexpected API handler failure")
        return error(500, "internal_error", "Unexpected server error")
