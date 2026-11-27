"""Serve the packaged demo assets through API Gateway."""

from importlib.resources import files
import re

ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def web_response(method, path, stage=""):
    asset = ASSETS.get(path)
    if method != "GET" or asset is None:
        return None

    name, content_type = asset
    body = (files("api") / "web" / name).read_text(encoding="utf-8")
    if path == "/" and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", stage):
        body = body.replace('href="/app.css"', f'href="/{stage}/app.css"')
        body = body.replace('src="/app.js"', f'src="/{stage}/app.js"')
    return {
        "statusCode": 200,
        "headers": {
            "content-type": content_type,
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
            "content-security-policy": (
                "default-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            ),
        },
        "body": body,
    }
