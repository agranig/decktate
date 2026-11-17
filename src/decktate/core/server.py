"""Local HTTP endpoint for Claude Code hooks and the status line.

Loopback only, and every request must carry the shared token. A held hook
request simply keeps its HTTP response open (one thread each).
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .hub import Hub

# request header -> key under which the session's terminal environment is stored
ENV_HEADERS = {
    "x-term-program": "TERM_PROGRAM", "x-iterm-session": "ITERM_SESSION_ID", "x-term-session": "TERM_SESSION_ID",
    "x-tmux": "TMUX", "x-tmux-pane": "TMUX_PANE", "x-kitty-window": "KITTY_WINDOW_ID",
    "x-kitty-listen": "KITTY_LISTEN_ON", "x-wezterm-pane": "WEZTERM_PANE", "x-tty": "TTY",
}


class HookServer:
    def __init__(self, hub: Hub, token: str, port: int = 47873, host: str = "127.0.0.1"):
        self.hub, self.token = hub, token
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code: int, body: dict | None = None):
                raw = json.dumps(body).encode() if body else b""
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass  # Claude Code gave up waiting (e.g. answered in the terminal)

            def _authorized(self) -> bool:
                given = self.headers.get("X-Decktate-Token") or self.headers.get("Authorization", "").removeprefix("Bearer ")
                return hmac.compare_digest(given.strip(), server.token)

            def _json(self) -> dict | None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    return body if isinstance(body, dict) else None
                except (ValueError, OSError):
                    return None

            def do_GET(self):
                if self.path == "/health":
                    return self._send(200, {"ok": True, "attached": server.hub.attached,
                                            "sessions": len(server.hub.sessions)})
                self._send(404)

            def do_POST(self):
                if not self._authorized():
                    return self._send(401, {"error": "bad token"})
                payload = self._json()
                if payload is None:
                    return self._send(400, {"error": "expected a JSON object"})
                if self.path == "/hook":
                    env = {name: self.headers.get(h, "") for h, name in ENV_HEADERS.items()}
                    try:
                        response = server.hub.handle(payload, env)
                    except Exception:
                        response = {}  # a bug here must never block or decide anything
                    return self._send(200, response or None)
                if self.path == "/statusline":
                    server.hub.statusline(payload)
                    return self._send(200)
                self._send(404)

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]

    def start(self) -> None:
        threading.Thread(target=self._httpd.serve_forever, daemon=True, name="hooks").start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
