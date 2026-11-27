"""Serve the demo interface on localhost and forward its API calls to API Gateway."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

CLIENT = Path(__file__).resolve().parent
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
EVENT = r"[a-z0-9-]+"
ROUTES = {
    "GET": re.compile(rf"/(?:events(?:/{EVENT}/(?:seats|stats))?|orders/[0-9a-fA-F]{{32}})"),
    "POST": re.compile(rf"/(?:events/{EVENT}/holds(?:/h-[0-9a-f]{{12}}/checkout)?|tickets/scan)"),
}


class DemoHandler(BaseHTTPRequestHandler):
    def send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def error(self, status, code, message):
        self.send(status, "application/json", json.dumps({"error": code, "message": message}).encode())

    def do_GET(self):
        if self.path in STATIC:
            name, kind = STATIC[self.path]
            self.send(200, kind, (CLIENT / "web" / name).read_bytes())
        else:
            self.proxy()

    def do_POST(self):
        self.proxy()

    def proxy(self):
        parsed = urlsplit(self.path)
        route = parsed.path.removeprefix("/api") if parsed.path.startswith("/api/") else parsed.path
        if not ROUTES[self.command].fullmatch(route) or (parsed.query and route != "/events"):
            self.error(404, "not_found", "Unknown demo route")
            return

        data = None
        headers = {}
        if self.command == "POST":
            if self.headers.get("Origin") != self.server.origin:
                self.error(403, "invalid_origin", "Open the demo at its localhost address")
                return
            size = self.headers.get("Content-Length", "")
            if not size.isascii() or not size.isdecimal() or len(size) > 4 or int(size) > 4096:
                self.error(400, "invalid_body", "Invalid request body length")
                return
            data = self.rfile.read(int(size))
            headers["Content-Type"] = "application/json"
            for header in ("X-Buyer-Id", "Idempotency-Key"):
                if header in self.headers:
                    headers[header] = self.headers[header]
        url = self.server.api_url + route
        if parsed.query:
            url += "?" + parsed.query
        req = Request(url, data=data, headers=headers, method=self.command)
        try:
            with urlopen(req, timeout=12) as upstream:
                status, payload = upstream.status, upstream.read()
        except HTTPError as error:
            status, payload = error.code, error.read()
        except (OSError, URLError):
            self.error(502, "upstream_unavailable", "Could not reach the AWS API. Try again.")
            return
        try:
            json.loads(payload)
        except (ValueError, UnicodeError):
            self.error(502, "invalid_upstream_response", "The AWS API did not return JSON")
            return
        self.send(status, "application/json", payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="ticketing")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.prefix):
        parser.error("invalid resource prefix")
    config = CLIENT.parent / "config" / f"{args.prefix}.json"
    try:
        api_url = json.loads(config.read_text(encoding="utf-8"))["api_url"].rstrip("/")
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        parser.error(f"could not read the deployment API URL from {config}: {exc}")
    if not re.fullmatch(r"https://[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com(?:/[A-Za-z0-9_-]{1,128})?", api_url):
        parser.error("expected an HTTPS API Gateway URL in local configuration")

    with ThreadingHTTPServer(("127.0.0.1", args.port), DemoHandler) as server:
        server.api_url = api_url
        server.origin = f"http://127.0.0.1:{server.server_port}"
        print(f"Ticketing demo: {server.origin} (Ctrl+C to stop)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
