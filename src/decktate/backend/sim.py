"""Simulated deck: keeps key images in memory, renders the grid to a PNG,
and optionally serves a small local web page that accepts key events."""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from .base import DeckBackend, KeyCallback

GAP = 14  # px between keys in the composite, roughly like the real bezel


class SimDeck(DeckBackend):
    def __init__(self, rows: int = 3, cols: int = 5, key_size: tuple[int, int] = (72, 72)):
        self.rows, self.cols, self.key_size = rows, cols, key_size
        self.images: list[Image.Image] = [
            Image.new("RGB", key_size, "black") for _ in range(rows * cols)
        ]
        self.brightness = 60
        self.version = 0  # bumped on every image change
        self._cb: KeyCallback | None = None
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # -- DeckBackend -------------------------------------------------------
    def set_key_image(self, key: int, image: Image.Image) -> None:
        with self._lock:
            self.images[key] = image.convert("RGB").resize(self.key_size)
            self.version += 1

    def set_brightness(self, percent: int) -> None:
        self.brightness = percent

    def on_key(self, callback: KeyCallback) -> None:
        self._cb = callback

    def close(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    # -- test / CLI input --------------------------------------------------
    def press(self, key: int) -> None:
        if self._cb:
            self._cb(key, True)

    def release(self, key: int) -> None:
        if self._cb:
            self._cb(key, False)

    # -- output ------------------------------------------------------------
    def snapshot(self, scale: int = 1) -> Image.Image:
        w, h = self.key_size
        img = Image.new(
            "RGB",
            (self.cols * w + (self.cols + 1) * GAP, self.rows * h + (self.rows + 1) * GAP),
            (12, 12, 14),
        )
        with self._lock:
            for i, key in enumerate(self.images):
                r, c = divmod(i, self.cols)
                img.paste(key, (GAP + c * (w + GAP), GAP + r * (h + GAP)))
        if scale != 1:
            img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
        return img

    def save(self, path: str, scale: int = 2) -> None:
        self.snapshot(scale).save(path)

    # -- web page ----------------------------------------------------------
    def serve(self, port: int = 8472, host: str = "127.0.0.1") -> str:
        deck = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, ctype, body: bytes):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/deck.png"):
                    buf = io.BytesIO()
                    deck.snapshot(2).save(buf, "PNG")
                    self._send(200, "image/png", buf.getvalue())
                elif self.path.startswith("/version"):
                    self._send(200, "application/json", json.dumps({"v": deck.version}).encode())
                elif self.path == "/":
                    self._send(200, "text/html", _page(deck).encode())
                else:
                    self._send(404, "text/plain", b"not found")

            def do_POST(self):
                # /key/<n>/down | /key/<n>/up
                parts = self.path.strip("/").split("/")
                try:
                    key, what = int(parts[1]), parts[2]
                    assert parts[0] == "key" and 0 <= key < deck.key_count
                except Exception:
                    return self._send(400, "text/plain", b"bad request")
                (deck.press if what == "down" else deck.release)(key)
                self._send(204, "text/plain", b"")

        self._server = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return f"http://{host}:{self._server.server_address[1]}/"


def _page(deck: SimDeck) -> str:
    w, h = deck.key_size
    return f"""<!doctype html><meta charset=utf-8><title>SimDeck</title>
<style>body{{background:#1b1b1f;color:#aaa;font:13px system-ui;display:grid;place-items:center;height:100vh;margin:0}}
#d{{position:relative;border-radius:18px;overflow:hidden;box-shadow:0 10px 40px #000}}
#d img{{display:block;user-select:none;-webkit-user-drag:none}}
.k{{position:absolute;width:{w*2}px;height:{h*2}px;border-radius:10px;cursor:pointer}}
.k:active{{outline:3px solid #fff8}}</style>
<div><div id=d><img id=i src=/deck.png></div><p>click = tap, press and hold = hold</p></div>
<script>
const d=document.getElementById('d'),G={GAP*2},W={w*2},H={h*2};
for(let n=0;n<{deck.key_count};n++){{const k=document.createElement('div');k.className='k';
 k.style.left=(G+(n%{deck.cols})*(W+G))+'px';k.style.top=(G+Math.floor(n/{deck.cols})*(H+G))+'px';
 k.onpointerdown=()=>fetch('/key/'+n+'/down',{{method:'POST'}});
 k.onpointerup=k.onpointerleave=e=>{{if(e.type=='pointerup'||e.buttons)fetch('/key/'+n+'/up',{{method:'POST'}})}};
 d.appendChild(k)}}
let v=-1;setInterval(async()=>{{const r=await(await fetch('/version')).json();
 if(r.v!=v){{v=r.v;document.getElementById('i').src='/deck.png?'+v}}}},120);
</script>"""
