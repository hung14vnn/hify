from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8089"


def main() -> int:
    identifier = sys.argv[1] if len(sys.argv) > 1 else "admin"
    password = sys.argv[2] if len(sys.argv) > 2 else "admin1"
    with httpx.Client(base_url=BASE, timeout=60.0, trust_env=False) as client:
        login = client.post(
            "/api/v1/auth/login", json={"identifier": identifier, "password": password}
        )
        print("login", login.status_code, login.text[:160])
        if login.status_code != 200:
            return 1
        token = login.json()["token"]
        params = {"apiKey": token, "v": "1.16.1", "c": "smoke", "f": "json"}
        for path in ("ping", "getUser", "getStarred2", "getPlaylists", "getLastfmStatus"):
            response = client.get(f"/rest/{path}", params=params)
            print(path, response.status_code, response.text[:220])
        search = client.get(
            "/rest/search3",
            params={**params, "query": "Son Tung M-TP", "songCount": 3, "artistCount": 2},
        )
        print("search3", search.status_code, search.text[:400])
        # First song of the search: FLAC bridge (off) -> YouTube Music fallback.
        try:
            songs = search.json()["subsonic-response"]["searchResult3"].get("song") or []
        except (KeyError, TypeError):
            songs = []
        if songs:
            song_id = songs[0]["id"]
            head = client.get(
                "/rest/stream",
                params={**params, "id": song_id},
                headers={"Range": "bytes=0-1023"},
                timeout=60.0,
            )
            print(
                "stream",
                song_id,
                head.status_code,
                head.headers.get("content-type"),
                head.headers.get("content-range"),
                len(head.content),
            )
            client.get("/rest/scrobble", params={**params, "id": song_id, "submission": "true"})
            starred = client.get("/rest/star", params={**params, "id": song_id})
            print("star", starred.status_code)
            liked = client.get("/rest/getStarred2", params=params)
            print("starred2", json.dumps(liked.json())[:300])

            created = client.get(
                "/rest/createPlaylist", params={**params, "name": "Smoke", "songId": song_id}
            )
            playlist = created.json()["subsonic-response"].get("playlist") or {}
            playlist_id = playlist.get("id")
            print("playlist", created.status_code, playlist_id, playlist.get("songCount"))
            if playlist_id:
                fetched = client.get("/rest/getPlaylist", params={**params, "id": playlist_id})
                print("getPlaylist", fetched.status_code, len((fetched.json()["subsonic-response"].get("playlist") or {}).get("entry") or []))
                client.get("/rest/deletePlaylist", params={**params, "id": playlist_id})
            client.get(
                "/rest/savePlayQueue",
                params={**params, "id": song_id, "current": song_id, "position": 12.5},
            )
            queue = client.get("/rest/getPlayQueue", params=params)
            print("queue", queue.status_code, queue.text[:240])
        else:
            print("stream skipped: no search results")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
