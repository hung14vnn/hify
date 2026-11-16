"""Fake Eclipse add-on for local bridge tests (direct FLAC only)."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

FIXTURE = Path(__file__).parent / "fixtures" / "test-tone.flac"
M4A_FIXTURE = Path(__file__).parent / "fixtures" / "test-tone.m4a"
PORT = 8090


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
        sys.stderr.write("fake-addon: " + (fmt % args) + "\n")

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path == "/manifest.json":
            self._json({"name": "Fake Add-on", "resources": ["resolve", "search", "stream"]})
            return
        if parts.path == "/resolve":
            title = (query.get("title") or [""])[0]
            artist = (query.get("artist") or [""])[0]
            duration = (query.get("durationMs") or ["0"])[0]
            self._json(
                {
                    "item": {
                        "id": "t1",
                        "type": "track",
                        "title": title,
                        "artist": artist,
                        "durationMs": int(duration or 0),
                    }
                }
            )
            return
        if parts.path == "/search":
            self._json({"tracks": []})
            return
        if parts.path == "/stream/t1":
            quality = (query.get("quality") or ["HI_RES_LOSSLESS"])[0].upper()
            if quality in {"HIGH", "LOW"}:
                self._json(
                    {
                        "url": f"http://127.0.0.1:{PORT}/audio.m4a",
                        "format": "m4a",
                        "container": "mp4",
                        "codec": "aac",
                        "expiresAt": 4102444800000,
                    }
                )
            else:
                self._json(
                    {
                        "url": f"http://127.0.0.1:{PORT}/audio.flac",
                        "format": "flac",
                        "container": "flac",
                        "codec": "flac",
                        "bitDepth": 24,
                        "sampleRate": 96000,
                        "expiresAt": 4102444800000,
                    }
                )
            return
        if parts.path in {"/audio.flac", "/audio.m4a"}:
            path = M4A_FIXTURE if parts.path.endswith(".m4a") else FIXTURE
            content_type = "audio/mp4" if path is M4A_FIXTURE else "audio/flac"
            data = path.read_bytes()
            total = len(data)
            header = self.headers.get("Range")
            start, end = 0, total - 1
            status = 200
            if header and header.startswith("bytes="):
                first, _, last = header[6:].partition("-")
                start = int(first or 0)
                end = min(int(last), total - 1) if last else total - 1
                status = 206
            chunk = data[start : end + 1]
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(chunk)))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()
            self.wfile.write(chunk)
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
