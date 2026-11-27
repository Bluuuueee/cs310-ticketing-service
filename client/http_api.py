"""JSON HTTP requests for the local smoke tests."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(url, body=None, buyer=None, idempotency_key=None):
    if body is None:
        req = Request(url)
    else:
        headers = {"Content-Type": "application/json"}
        if buyer is not None:
            headers["X-Buyer-Id"] = buyer
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        req = Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
    try:
        with urlopen(req, timeout=15) as result:
            return result.status, json.load(result)
    except HTTPError as error:
        return error.code, json.load(error)
