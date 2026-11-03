"""Webhook service: monitoring tools POST alerts, the service correlates them in memory.

    POST /events      JSON object or array of alerts      -> {"accepted": n, "rejected": m}
    GET  /incidents   current incident payloads
    GET  /metrics     noise-reduction metrics
    GET  /health      liveness

If EVENT_API_TOKEN is set, POST /events requires "Authorization: Bearer <token>".
Run: event-corr-serve --topology sample_data/topology.json --port 8000
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Correlator
from .topology import load_topology

log = logging.getLogger(__name__)
MAX_BODY = 1_000_000  # 1 MB per request


def make_handler(correlator: Correlator, token: str | None):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "event-corr/1.1"

        def _send(self, status: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            with lock:
                if self.path == "/health":
                    return self._send(200, {"status": "ok"})
                if self.path == "/incidents":
                    return self._send(200, correlator.incidents())
                if self.path == "/metrics":
                    return self._send(200, correlator.metrics())
            return self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/events":
                return self._send(404, {"error": "not found"})
            if token and not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return self._send(401, {"error": "unauthorized"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self._send(413 if length > MAX_BODY else 400, {"error": "body must be 1 byte to 1 MB"})
            try:
                payload = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})
            events = payload if isinstance(payload, list) else [payload]
            with lock:
                before = len(correlator.rejected)
                correlator.ingest(events)
                rejected = len(correlator.rejected) - before
            return self._send(202, {"accepted": len(events) - rejected, "rejected": rejected})

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def build_server(topology_path: str, host: str = "127.0.0.1", port: int = 8000, window_min: int = 10,
                 token: str | None = None) -> ThreadingHTTPServer:
    topo = load_topology(topology_path)
    correlator = Correlator(topo.ci_by_host, topo.parent_of, topo.service_tier,
                            window=timedelta(minutes=window_min), maintenance=topo.maintenance)
    return ThreadingHTTPServer((host, port), make_handler(correlator, token))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="event-corr-serve", description="Alert correlation webhook service")
    ap.add_argument("--topology", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--window-min", type=int, default=10)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    server = build_server(args.topology, args.host, args.port, args.window_min, os.environ.get("EVENT_API_TOKEN"))
    log.info("listening on http://%s:%d", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
