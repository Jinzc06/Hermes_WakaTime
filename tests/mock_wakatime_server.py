#!/usr/bin/env python3
"""Minimal WakaTime-compatible mock server for testing the plugin locally.

Captures POSTs to /api/v1/users/current/heartbeats into ``heartbeats.jsonl``
(next to this script, or $MOCK_OUTPUT), validates the Basic auth header, and
responds 201 — mirroring the real WakaTime API contract.

Usage:
    python3 tests/mock_wakatime_server.py [port] [api-key]

Defaults: port 18765, api key "test-key".
"""
from __future__ import annotations

import base64
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 18765
OUTPUT = Path(__file__).parent / "heartbeats.jsonl"
HEARTBEAT_PATH = "/api/v1/users/current/heartbeats"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _auth_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        token = header[6:]
        key = self.server.api_key  # type: ignore[attr-defined]
        expected = {
            base64.b64encode(key.encode("utf-8")).decode("ascii"),
            base64.b64encode((key + ":").encode("utf-8")).decode("ascii"),
        }
        return token in expected

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != HEARTBEAT_PATH:
            self._send(404, {"error": "not found"})
            return
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"error": "invalid json"})
            return
        with open(OUTPUT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
        self._send(201, {"data": payload if isinstance(payload, list) else [payload]})


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    api_key = sys.argv[2] if len(sys.argv) > 2 else "test-key"
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.api_key = api_key  # type: ignore[attr-defined]
    print(f"mock wakatime server on http://{HOST}:{port} (api key: {api_key!r})", flush=True)
    print(f"heartbeats -> {OUTPUT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
