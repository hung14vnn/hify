from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8091"
SONG = sys.argv[2] if len(sys.argv) > 2 else ""


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=90.0, trust_env=False) as client:
        token = client.post(
            "/api/v1/auth/login", json={"identifier": "admin", "password": "admin1"}
        ).json()["token"]
        params = {"apiKey": token, "v": "1.16.1", "c": "yt-probe", "f": "json"}
        song_id = SONG
        if not song_id:
            hits = client.get("/rest/getTodaysHits", params={**params, "count": 1})
            songs = hits.json()["subsonic-response"]["todaysHits"]["song"]
            if not songs:
                print("no yt songs")
                return 1
            song_id = songs[0]["id"]
            print("picked", song_id, songs[0].get("title"), songs[0].get("artist"))
        response = client.get(
            "/rest/stream",
            params={**params, "id": song_id},
            headers={"Range": "bytes=0-1023"},
        )
        print(
            "stream",
            song_id,
            response.status_code,
            response.headers.get("content-type"),
            response.headers.get("content-range"),
            len(response.content),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
