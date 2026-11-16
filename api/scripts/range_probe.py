from __future__ import annotations

import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8089"
RANGES = ["bytes=0-1", "bytes=0-", "bytes=0-1023", "bytes=1024-2047"]


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=90.0, trust_env=False) as client:
        token = client.post(
            "/api/v1/auth/login", json={"identifier": "admin", "password": "admin1"}
        ).json()["token"]
        params = {"apiKey": token, "v": "1.16.1", "c": "probe", "f": "json"}
        search = client.get(
            "/rest/search3", params={**params, "query": "Son Tung M-TP", "songCount": 1}
        )
        song_id = search.json()["subsonic-response"]["searchResult3"]["song"][0]["id"]
        print("song", song_id)
        for value in RANGES:
            started = time.time()
            response = client.get(
                "/rest/stream",
                params={**params, "id": song_id},
                headers={"Range": value},
                timeout=90.0,
            )
            print(
                value,
                response.status_code,
                response.headers.get("content-type"),
                response.headers.get("content-range"),
                len(response.content),
                f"{time.time() - started:.1f}s",
            )
            time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
