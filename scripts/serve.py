#!/usr/bin/env python3
"""Local HTTP server for the DLP dashboards with a /api/refresh endpoint.

Serves the static dashboards from the repo root and exposes one POST endpoint:

    POST /api/refresh   →  runs fetch_initiatives.py + splice_dashboard.py
                           and returns JSON {ok, log, ms} on completion.

Usage:
    python3 scripts/serve.py            # default :8765
    python3 scripts/serve.py --port 9000

Open http://127.0.0.1:8765/ in a browser. The Refresh button in
pi-dashboard.html will POST here.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FETCH = SCRIPTS / "fetch_initiatives.py"
SPLICE = SCRIPTS / "splice_dashboard.py"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_HEAD(self):
        # The dashboard's Refresh button probes HEAD /api/refresh to detect
        # whether a refresh backend is available. Reply 200 so the button can
        # un-hide itself; any other handler does the SimpleHTTPRequestHandler
        # default (file lookup).
        if self.path.rstrip("/") == "/api/refresh":
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        super().do_HEAD()

    def do_POST(self):
        if self.path.rstrip("/") != "/api/refresh":
            self.send_error(404, "Unknown endpoint")
            return
        # Drain request body if any
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

        t0 = time.monotonic()
        log_lines: list[str] = []
        ok = True
        for step, script in (("fetch", FETCH), ("splice", SPLICE)):
            log_lines.append(f"$ python3 {script.relative_to(ROOT)}")
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            if proc.stdout:
                log_lines.append(proc.stdout.rstrip())
            if proc.stderr:
                log_lines.append(proc.stderr.rstrip())
            if proc.returncode != 0:
                ok = False
                log_lines.append(f"[{step} exited {proc.returncode}]")
                break

        ms = int((time.monotonic() - t0) * 1000)
        payload = json.dumps({"ok": ok, "log": "\n".join(log_lines), "ms": ms}).encode()
        self.send_response(200 if ok else 500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        # Best-effort no-cache so the page sees fresh HTML after refresh
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def end_headers(self):
        # Disable caching for HTML so refresh-then-reload picks up new content
        if self.path.endswith(".html") or self.path.endswith("/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if not FETCH.exists() or not SPLICE.exists():
        sys.exit("fetch_initiatives.py / splice_dashboard.py missing under scripts/")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((args.host, args.port), Handler) as httpd:
        url = f"http://{args.host}:{args.port}/"
        print(f"Serving dashboards from {ROOT} at {url}", file=sys.stderr)
        print("POST /api/refresh runs fetch + splice. Ctrl-C to stop.", file=sys.stderr)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
