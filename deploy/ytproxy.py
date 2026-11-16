"""Minimal HTTP CONNECT proxy for the ProtonVPN network namespace.

Runs inside the gluetun container's network namespace so YouTube sees the
VPN address. Only CONNECT is implemented; yt-dlp and httpx use it for HTTPS.
"""

from __future__ import annotations

import select
import socket
import socketserver

LISTEN = ("0.0.0.0", 8117)
IDLE_TIMEOUT = 300


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(30)
        data = b""
        while b"\r\n\r\n" not in data:
            try:
                chunk = client.recv(4096)
            except OSError:
                return
            if not chunk or len(data) > 65536:
                return
            data += chunk
        head, rest = data.split(b"\r\n\r\n", 1)
        try:
            method, target, _ = head.split(b"\r\n", 1)[0].decode("latin1").split(" ", 2)
        except ValueError:
            return
        if method.upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            host, port = target.rsplit(":", 1)
            upstream = socket.create_connection((host, int(port)), timeout=20)
        except (OSError, ValueError):
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if rest:
            upstream.sendall(rest)
        sockets = [client, upstream]
        try:
            while True:
                readable, _, failed = select.select(sockets, [], sockets, IDLE_TIMEOUT)
                if failed or not readable:
                    return
                for source in readable:
                    try:
                        chunk = source.recv(256 * 1024)
                    except OSError:
                        return
                    if not chunk:
                        return
                    (upstream if source is client else client).sendall(chunk)
        finally:
            try:
                upstream.close()
            except OSError:
                pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(LISTEN, Handler) as server:
        print(f"ytproxy listening on {LISTEN[0]}:{LISTEN[1]}", flush=True)
        server.serve_forever()
