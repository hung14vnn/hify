"""Import the old hify library into the new hify database.

Keeps accounts (bcrypt), playlists, and the downloaded-library metadata.
Local files are resolved to Spotify or YouTube Music ids so they stay
playable; anything that cannot be matched is reported instead of guessed.

Run from ``api/`` so the package imports resolve::

    python scripts/migrate_hify.py --old-db ../legacy/library.db --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import difflib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from models import song_from_spotify, song_from_yt  # noqa: E402
from providers.spotapi_client import SpotApiClient  # noqa: E402
from providers.ytmusic_stream_service import YTMusicStreamService  # noqa: E402


def fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    text = re.sub(r"\((?:[^)]*)\)|\[[^\]]*\]", " ", text)
    text = re.sub(r"\s+-\s+.*$", " ", text)
    text = re.sub(r"[^0-9a-z\u00c0-\u1ef9]+", " ", text)
    return " ".join(text.split())


def ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def artist_score(target: str, candidates: list[str]) -> float:
    target_folded = fold(target)
    if not target_folded:
        return 0.0
    best = 0.0
    for name in candidates:
        candidate = fold(name)
        if not candidate:
            continue
        if candidate in target_folded or target_folded in candidate:
            best = max(best, 1.0)
            continue
        target_tokens = set(target_folded.split())
        candidate_tokens = set(candidate.split())
        if target_tokens and candidate_tokens:
            best = max(best, len(target_tokens & candidate_tokens) / len(target_tokens))
    return best


def epoch(value: Any, default: int | None = None) -> int:
    if value is None or value == "":
        return int(default if default is not None else time.time())
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return int(default if default is not None else time.time())


def clean_duration(value: Any) -> int | None:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def payload_from_yt(
    video_id: str,
    *,
    title: str,
    artist: str,
    album: str,
    duration: int | None,
    cover: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"yt-{video_id}",
        "title": title,
        "album": album,
        "artist": artist or "Unknown Artist",
        "displayArtist": artist or None,
        "coverArt": cover,
        "duration": duration,
        "suffix": "opus",
        "contentType": "audio/ogg",
        "type": "music",
        "isDir": False,
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


class Resolver:
    def __init__(self, cache_path: Path, http: httpx.AsyncClient) -> None:
        self.spotify = SpotApiClient()
        self.ytmusic = YTMusicStreamService(http)
        self.cache_path = cache_path
        self.cache: dict[str, dict[str, Any] | None] = {}
        if cache_path.is_file():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self.cache = {}
        self.stats = {"cache": 0, "spotify": 0, "youtube": 0, "unmatched": 0}
        self.unmatched: list[dict[str, str]] = []
        self._dirty = 0

    async def _spotify_tracks(self, query: str) -> list[dict[str, Any]]:
        try:
            tracks, _ = await self.spotify.search_tracks(query, limit=6)
            return tracks
        except Exception as exc:  # noqa: BLE001 - provider outages are reported
            print(f"  spotify search failed: {exc}", flush=True)
            return []

    async def resolve(
        self,
        title: str,
        artist: str,
        album: str,
        duration: int | None,
        *,
        cover: str | None = None,
    ) -> dict[str, Any] | None:
        key = f"{fold(title)}|{fold(artist)}"
        if key in self.cache:
            self.stats["cache"] += 1
            return self.cache[key]

        query = " ".join(part for part in (title, artist) if part).strip()
        target_title = fold(title)
        best: tuple[float, dict[str, Any]] | None = None

        for item in await self._spotify_tracks(query):
            name = str(item.get("name") or "")
            artists = [
                str(entry.get("name") or "")
                for entry in item.get("artists") or []
                if isinstance(entry, dict)
            ]
            candidate_seconds = clean_duration((item.get("duration_ms") or 0) / 1000)
            score = (
                ratio(target_title, fold(name)) * 0.65
                + artist_score(artist, artists) * 0.25
                + (
                    max(0.0, 1 - abs(candidate_seconds - duration) / 30)
                    if duration and candidate_seconds
                    else 0.5
                )
                * 0.10
            )
            if ratio(target_title, fold(name)) >= 0.72 and artist_score(artist, artists) >= 0.35:
                if best is None or score > best[0]:
                    best = (score, song_from_spotify(item))
        if best is not None:
            self.stats["spotify"] += 1
            self.cache[key] = best[1]
            self._dirty += 1
            await self._flush()
            return best[1]

        youtube_best: tuple[float, dict[str, Any]] | None = None
        try:
            yt_tracks = await self.ytmusic.search_tracks(query, limit=6)
        except Exception as exc:  # noqa: BLE001
            print(f"  youtube search failed: {exc}", flush=True)
            yt_tracks = []
        for item in yt_tracks:
            name = str(item.get("title") or "")
            artists = [
                str(entry.get("name") or "")
                for entry in item.get("artists") or []
                if isinstance(entry, dict)
            ]
            candidate_seconds = clean_duration(item.get("duration_seconds"))
            score = (
                ratio(target_title, fold(name)) * 0.65
                + artist_score(artist, artists) * 0.25
                + (
                    max(0.0, 1 - abs(candidate_seconds - duration) / 30)
                    if duration and candidate_seconds
                    else 0.5
                )
                * 0.10
            )
            if ratio(target_title, fold(name)) >= 0.72 and artist_score(artist, artists) >= 0.30:
                if youtube_best is None or score > youtube_best[0]:
                    payload = song_from_yt(item)
                    payload.setdefault("album", album)
                    if cover and not payload.get("coverArt"):
                        payload["coverArt"] = cover
                    youtube_best = (score, payload)
        if youtube_best is not None:
            self.stats["youtube"] += 1
            self.cache[key] = youtube_best[1]
            self._dirty += 1
            await self._flush()
            return youtube_best[1]

        self.stats["unmatched"] += 1
        self.unmatched.append(
            {"title": title, "artist": artist, "album": album, "reason": "no confident match"}
        )
        self.cache[key] = None
        self._dirty += 1
        await self._flush()
        return None

    async def _flush(self) -> None:
        if self._dirty < 10:
            return
        self._dirty = 0
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def save(self) -> None:
        self._dirty = 0
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )


def read_old(path: Path) -> dict[str, list[dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(sql)]

    data = {
        "users": rows("SELECT * FROM auth_users"),
        "providers": rows("SELECT * FROM auth_providers WHERE provider = 'local'"),
        "playlists": rows("SELECT * FROM library_playlists ORDER BY created_at"),
        "playlist_items": rows(
            "SELECT * FROM library_playlist_tracks ORDER BY playlist_id, position"
        ),
        "local_tracks": rows("SELECT * FROM local_tracks ORDER BY imported_at"),
        "track_identities": rows("SELECT * FROM local_track_external_identities"),
        "youtube_links": rows("SELECT * FROM youtube_track_links"),
        "download_tasks": rows("SELECT * FROM download_tasks ORDER BY created_at"),
        "play_history": rows("SELECT * FROM library_play_history ORDER BY played_at"),
        "selections": rows("SELECT * FROM library_user_selections ORDER BY selected_at"),
        "history_users": rows("SELECT DISTINCT user_id FROM library_play_history"),
        "selection_users": rows("SELECT DISTINCT user_id FROM library_user_selections"),
    }
    conn.close()
    return data


def snapshot(old: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
    dest = sqlite3.connect(str(target))
    with dest:
        source.backup(dest)
    dest.close()
    source.close()


def playlist_payload(
    item: dict[str, Any], resolved: dict[str, Any] | None
) -> dict[str, Any] | None:
    if item.get("source_type") == "ytmusic" and item.get("track_source_id"):
        return payload_from_yt(
            str(item["track_source_id"]),
            title=str(item.get("track_name") or ""),
            artist=str(item.get("artist_name") or ""),
            album=str(item.get("album_name") or ""),
            duration=clean_duration(item.get("duration")),
            cover=item.get("cover_url"),
        )
    if resolved is not None:
        if item.get("cover_url") and not resolved.get("coverArt"):
            resolved = dict(resolved, coverArt=item["cover_url"])
        return resolved
    return None


def best_track(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    unique = {str(track["id"]): track for track in candidates}
    if not unique:
        return None
    return max(
        unique.values(),
        key=lambda track: (
            track.get("availability") == "indexed",
            track.get("file_format") == "flac",
            int(track.get("bit_depth") or 0),
            float(track.get("imported_at") or 0),
        ),
    )


async def run(args: argparse.Namespace) -> int:
    old_path = Path(args.old_db).resolve()
    new_path = Path(args.new_db).resolve()
    if not old_path.is_file():
        print(f"old database not found: {old_path}")
        return 2

    snap_path = Path(args.snapshot).resolve() if args.snapshot else new_path.with_name(
        "legacy-library-snapshot.db"
    )
    source_path = old_path
    if not args.dry_run:
        snapshot(old_path, snap_path)
        source_path = snap_path
        print(f"snapshot: {snap_path}")
    data = read_old(source_path)
    print(
        "old data: "
        f"{len(data['users'])} users, {len(data['playlists'])} playlists, "
        f"{len(data['playlist_items'])} playlist items, "
        f"{len(data['local_tracks'])} local tracks, {len(data['download_tasks'])} downloads"
    )

    providers: dict[str, dict[str, Any]] = {}
    for provider in data["providers"]:
        try:
            providers[str(provider["user_id"])] = json.loads(provider.get("provider_data") or "{}")
        except json.JSONDecodeError:
            providers[str(provider["user_id"])] = {}

    users: list[dict[str, Any]] = []
    for user in data["users"]:
        raw = providers.get(str(user["id"]), {})
        password_hash = str(raw.get("password_hash") or "")
        if not password_hash:
            print(f"skip user without local password: {user.get('username')}")
            continue
        username = str(user.get("username") or user.get("username_display") or user["id"])
        users.append(
            {
                "user_id": str(user["id"]),
                "username": username,
                "display_name": str(user.get("display_name") or username),
                "email": str(user.get("email") or ""),
                "password_hash": password_hash,
                "role": "admin" if user.get("role") == "admin" else "user",
                "created_at": epoch(user.get("created_at")),
            }
        )
    user_names = {user["user_id"]: user["username"] for user in users}

    local_by_id = {str(track["id"]): track for track in data["local_tracks"]}
    yt_links: dict[tuple[str, int, int], str] = {}
    for link in data["youtube_links"]:
        try:
            key = (
                str(link["album_id"]),
                int(link["disc_number"] or 1),
                int(link["track_number"] or 0),
            )
        except (TypeError, ValueError):
            continue
        yt_links[key] = str(link["video_id"])

    provider_tracks: dict[str, list[dict[str, Any]]] = {}
    for track in data["local_tracks"]:
        key = str(track.get("embedded_recording_mbid") or "").strip().casefold()
        if key:
            provider_tracks.setdefault(key, []).append(track)
    for identity in data["track_identities"]:
        track = local_by_id.get(str(identity.get("local_track_id") or ""))
        key = str(identity.get("recording_mbid") or "").strip().casefold()
        if track is not None and key:
            provider_tracks.setdefault(key, []).append(track)

    cache_path = Path(args.cache).resolve() if args.cache else new_path.with_name(
        "migration-cache.json"
    )
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(25.0, read=None),
        follow_redirects=True,
        trust_env=False,
        headers={"User-Agent": "hify-migrate/1.0"},
    )
    resolver = Resolver(cache_path, http)
    report: dict[str, Any] = {
        "old_db": str(old_path),
        "snapshot": str(snap_path),
        "new_db": str(new_path),
        "dry_run": args.dry_run,
        "users": {"imported": len(users)},
        "playlists": [],
        "downloads": {},
        "resolution": resolver.stats,
        "unmatched": resolver.unmatched,
    }

    items_by_playlist: dict[str, list[dict[str, Any]]] = {}
    for item in data["playlist_items"]:
        items_by_playlist.setdefault(str(item["playlist_id"]), []).append(item)

    imported_playlists: list[dict[str, Any]] = []
    for order, playlist in enumerate(data["playlists"]):
        owner = str(playlist.get("user_id") or "")
        if not owner:
            print(f"skip playlist without owner: {playlist.get('name')}")
            continue
        entries: list[dict[str, Any]] = []
        unresolved: list[dict[str, str]] = []
        for item in items_by_playlist.get(str(playlist["id"]), []):
            local = local_by_id.get(str(item.get("local_track_id") or ""))
            resolved: dict[str, Any] | None = None
            if item.get("source_type") == "local":
                title = str((local or {}).get("title") or item.get("track_name") or "")
                artist = str((local or {}).get("artist_name") or item.get("artist_name") or "")
                album = str((local or {}).get("album_title") or item.get("album_name") or "")
                duration = clean_duration(
                    (local or {}).get("duration_seconds") or item.get("duration")
                )
                video_id = None
                if local:
                    video_id = yt_links.get(
                        (
                            str(local.get("local_album_id") or ""),
                            int(local.get("disc_number") or 1),
                            int(local.get("track_number") or 0),
                        )
                    )
                if video_id:
                    resolved = payload_from_yt(
                        video_id,
                        title=title,
                        artist=artist,
                        album=album,
                        duration=duration,
                        cover=item.get("cover_url"),
                    )
                else:
                    resolved = await resolver.resolve(
                        title, artist, album, duration, cover=item.get("cover_url")
                    )
            payload = playlist_payload(item, resolved)
            if payload is None:
                unresolved.append(
                    {
                        "title": str(item.get("track_name") or ""),
                        "artist": str(item.get("artist_name") or ""),
                    }
                )
                continue
            entries.append(payload)
        imported_playlists.append(
            {
                "id": f"pl-legacy-{playlist['id']}",
                "user_id": owner,
                "name": str(playlist.get("name") or "Imported playlist"),
                "public": 1 if playlist.get("is_public") else 0,
                "owner": user_names.get(owner, ""),
                "created_at": epoch(playlist.get("created_at")),
                "updated_at": epoch(playlist.get("updated_at")),
                "entries": entries,
            }
        )
        report["playlists"].append(
            {
                "name": str(playlist.get("name") or ""),
                "user": user_names.get(owner, owner),
                "items": len(items_by_playlist.get(str(playlist["id"]), [])),
                "imported": len(entries),
                "unresolved": unresolved,
            }
        )
        print(
            f"playlist {order + 1}/{len(data['playlists'])}: "
            f"{playlist.get('name')} -> {len(entries)}/{len(items_by_playlist.get(str(playlist['id']), []))}",
            flush=True,
        )

    indexed = [track for track in data["local_tracks"] if track.get("availability") == "indexed"]
    resolved_local: dict[str, dict[str, Any] | None] = {}
    for number, track in enumerate(indexed, start=1):
        track_id = str(track["id"])
        title = str(track.get("title") or "")
        artist = str(track.get("artist_name") or "")
        album = str(track.get("album_title") or "")
        duration = clean_duration(track.get("duration_seconds"))
        video_id = yt_links.get(
            (
                str(track.get("local_album_id") or ""),
                int(track.get("disc_number") or 1),
                int(track.get("track_number") or 0),
            )
        )
        if video_id:
            resolved_local[track_id] = payload_from_yt(
                video_id, title=title, artist=artist, album=album, duration=duration, cover=None
            )
        else:
            resolved_local[track_id] = await resolver.resolve(
                title, artist, album, duration
            )
        if number % 20 == 0:
            print(f"library tracks: {number}/{len(indexed)}", flush=True)

    history_entries: list[dict[str, Any]] = []
    history_skipped = 0
    for row in data["play_history"]:
        owner = str(row.get("user_id") or "")
        if owner not in user_names:
            history_skipped += 1
            continue
        local = local_by_id.get(str(row.get("local_track_id") or ""))
        title = str((local or {}).get("title") or row.get("track_name") or "")
        artist = str((local or {}).get("artist_name") or row.get("artist_name") or "")
        album = str((local or {}).get("album_title") or row.get("album_name") or "")
        duration = clean_duration((local or {}).get("duration_seconds"))
        if duration is None and row.get("duration_ms") is not None:
            try:
                duration = clean_duration(float(row["duration_ms"]) / 1000)
            except (TypeError, ValueError):
                duration = None
        payload = resolved_local.get(str(local["id"])) if local is not None else None
        if payload is None:
            payload = await resolver.resolve(title, artist, album, duration)
        if payload is None:
            history_skipped += 1
            continue
        history_entries.append(
            {
                "user_id": owner,
                "track_id": payload["id"],
                "payload": payload,
                "played_at": epoch(row.get("played_at")),
            }
        )

    spotify_star_cache: dict[str, dict[str, Any] | None] = {}

    async def spotify_by_id(track_id: str) -> dict[str, Any] | None:
        if track_id not in spotify_star_cache:
            try:
                track = await resolver.spotify.get_track(track_id)
                spotify_star_cache[track_id] = (
                    song_from_spotify(track) if track.get("id") else None
                )
            except Exception:  # noqa: BLE001 - fall back to a metadata search
                spotify_star_cache[track_id] = None
        return spotify_star_cache[track_id]

    starred_entries: list[dict[str, Any]] = []
    starred_missing: list[dict[str, str]] = []
    for selection in data["selections"]:
        if selection.get("item_kind") != "track":
            continue
        owner = str(selection.get("user_id") or "")
        provider = str(selection.get("provider_id") or "").strip()
        if owner not in user_names or not provider:
            continue
        candidate = best_track(provider_tracks.get(provider.casefold(), []))
        if candidate is None:
            starred_missing.append({"user": user_names.get(owner, owner), "provider_id": provider})
            continue
        local_id = str(candidate["id"])
        title = str(candidate.get("title") or "")
        artist = str(candidate.get("artist_name") or "")
        album = str(candidate.get("album_title") or "")
        duration = clean_duration(candidate.get("duration_seconds"))
        original = str(candidate.get("embedded_recording_mbid") or "").strip()
        payload: dict[str, Any] | None = None
        lowered = original.casefold()
        if lowered.startswith("spotify:track:"):
            payload = await spotify_by_id(original.split(":", 2)[2])
        elif lowered.startswith("youtube:track:"):
            cover = (resolved_local.get(local_id) or {}).get("coverArt")
            payload = payload_from_yt(
                original.split(":", 2)[2],
                title=title,
                artist=artist,
                album=album,
                duration=duration,
                cover=cover,
            )
        if payload is None:
            payload = resolved_local.get(local_id)
        if payload is None:
            payload = await resolver.resolve(title, artist, album, duration)
        if payload is None:
            starred_missing.append({"user": user_names.get(owner, owner), "provider_id": provider})
            continue
        starred_entries.append(
            {
                "user_id": owner,
                "track_id": payload["id"],
                "payload": payload,
                "starred_at": epoch(selection.get("selected_at")),
            }
        )

    starred_unique: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in starred_entries:
        key = (entry["user_id"], entry["track_id"])
        previous = starred_unique.get(key)
        if previous is None or entry["starred_at"] > previous["starred_at"]:
            starred_unique[key] = entry
    starred_entries = list(starred_unique.values())

    legacy_downloads: list[dict[str, Any]] = []
    for track in data["local_tracks"]:
        resolved = resolved_local.get(str(track["id"]))
        legacy_downloads.append(
            {
                "id": f"dl-{track['id']}",
                "user_id": "",
                "track_id": (resolved or {}).get("id"),
                "title": str(track.get("title") or ""),
                "artist": str(track.get("artist_name") or ""),
                "album": str(track.get("album_title") or ""),
                "file_path": track.get("file_path"),
                "file_format": track.get("file_format"),
                "bit_rate": track.get("bit_rate"),
                "sample_rate": track.get("sample_rate"),
                "bit_depth": track.get("bit_depth"),
                "source": "library",
                "status": track.get("availability"),
                "created_at": epoch(track.get("imported_at")),
                "payload": json.dumps(resolved, ensure_ascii=False) if resolved else None,
            }
        )
    for task in data["download_tasks"]:
        title = str(task.get("track_title") or task.get("album_title") or "")
        artist = str(task.get("artist_name") or "")
        album = str(task.get("album_title") or "")
        resolved = None
        if task.get("track_title"):
            resolved = await resolver.resolve(
                title,
                artist,
                album,
                clean_duration(task.get("track_duration_seconds")),
                cover=task.get("cover_url"),
            )
        legacy_downloads.append(
            {
                "id": f"dt-{task['id']}",
                "user_id": str(task.get("user_id") or ""),
                "track_id": (resolved or {}).get("id"),
                "title": title,
                "artist": artist,
                "album": album,
                "file_path": task.get("final_path"),
                "file_format": task.get("quality_format"),
                "bit_rate": task.get("quality_bitrate"),
                "sample_rate": task.get("quality_sample_rate"),
                "bit_depth": task.get("quality_bit_depth"),
                "source": str(task.get("source") or "download"),
                "status": str(task.get("status") or ""),
                "created_at": epoch(task.get("created_at")),
                "payload": json.dumps(resolved, ensure_ascii=False) if resolved else None,
            }
        )

    download_entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for track in indexed:
        payload = resolved_local.get(str(track["id"]))
        if payload and payload["id"] not in seen_ids:
            seen_ids.add(payload["id"])
            download_entries.append(payload)

    active_users = (
        {str(playlist["user_id"]) for playlist in data["playlists"] if playlist.get("user_id")}
        | {str(task["user_id"]) for task in data["download_tasks"] if task.get("user_id")}
        | {str(row["user_id"]) for row in data["history_users"] if row.get("user_id")}
        | {str(row["user_id"]) for row in data["selection_users"] if row.get("user_id")}
    )
    download_playlists: list[dict[str, Any]] = []
    for user_id in sorted(active_users):
        if user_id not in user_names or not download_entries:
            continue
        download_playlists.append(
            {
                "id": f"pl-legacy-downloaded-{user_id}",
                "user_id": user_id,
                "name": "Downloaded (old hify)",
                "public": 0,
                "owner": user_names[user_id],
                "created_at": epoch(None),
                "updated_at": epoch(None),
                "entries": download_entries,
            }
        )

    report["downloads"] = {
        "library_tracks": len(data["local_tracks"]),
        "indexed": len(indexed),
        "resolved": len(download_entries),
        "tasks": len(data["download_tasks"]),
        "playlists": len(download_playlists),
    }
    report["history"] = {
        "rows": len(data["play_history"]),
        "resolved": len(history_entries),
        "skipped": history_skipped,
    }
    report["starred"] = {
        "selections": sum(1 for row in data["selections"] if row.get("item_kind") == "track"),
        "migrated": len(starred_entries),
        "unresolved": len(starred_missing),
        "unresolved_ids": starred_missing[:80],
    }
    resolver.save()
    await http.aclose()

    report_path = Path(args.report).resolve() if args.report else new_path.with_name(
        "migration-report.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.dry_run:
        print(
            f"dry run: {len(users)} users, {len(imported_playlists)} playlists, "
            f"{len(legacy_downloads)} download rows, {len(download_playlists)} downloaded playlists, "
            f"{len(history_entries)} history rows, {len(starred_entries)} starred tracks"
        )
        print(f"resolution: {resolver.stats}")
        print(f"report: {report_path}")
        return 0

    new_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(new_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(db.SCHEMA)
    history_inserted = 0
    starred_inserted = 0
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM legacy_users")
        conn.executemany(
            "INSERT INTO legacy_users"
            " (user_id, username, display_name, email, password_hash, role, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    user["user_id"],
                    user["username"],
                    user["display_name"],
                    user["email"],
                    user["password_hash"],
                    user["role"],
                    user["created_at"],
                )
                for user in users
            ],
        )
        conn.execute("DELETE FROM legacy_downloads")
        conn.executemany(
            "INSERT INTO legacy_downloads"
            " (id, user_id, track_id, title, artist, album, file_path, file_format,"
            " bit_rate, sample_rate, bit_depth, source, status, created_at, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["id"],
                    row["user_id"],
                    row["track_id"],
                    row["title"],
                    row["artist"],
                    row["album"],
                    row["file_path"],
                    row["file_format"],
                    row["bit_rate"],
                    row["sample_rate"],
                    row["bit_depth"],
                    row["source"],
                    row["status"],
                    row["created_at"],
                    row["payload"],
                )
                for row in legacy_downloads
            ],
        )
        for playlist in imported_playlists + download_playlists:
            conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist["id"],))
            conn.execute("DELETE FROM playlists WHERE id = ?", (playlist["id"],))
            conn.execute(
                "INSERT INTO playlists"
                " (id, user_id, name, comment, owner, public, cover, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    playlist["id"],
                    playlist["user_id"],
                    playlist["name"],
                    "Imported from old hify",
                    playlist["owner"],
                    playlist["public"],
                    playlist["created_at"],
                    playlist["updated_at"],
                ),
            )
            for position, payload in enumerate(playlist["entries"]):
                conn.execute(
                    "INSERT INTO playlist_items (playlist_id, position, track_id, payload)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        playlist["id"],
                        position,
                        payload["id"],
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
        existing_history = {
            tuple(row)
            for row in conn.execute(
                "SELECT user_id, track_id, played_at FROM play_history"
            )
        }
        for entry in history_entries:
            key = (entry["user_id"], entry["track_id"], entry["played_at"])
            if key in existing_history:
                continue
            conn.execute(
                "INSERT INTO play_history (user_id, track_id, payload, played_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    entry["user_id"],
                    entry["track_id"],
                    json.dumps(entry["payload"], ensure_ascii=False),
                    entry["played_at"],
                ),
            )
            existing_history.add(key)
            history_inserted += 1
        existing_stars = {
            tuple(row)
            for row in conn.execute("SELECT user_id, track_id FROM liked_tracks")
        }
        for entry in starred_entries:
            key = (entry["user_id"], entry["track_id"])
            if key in existing_stars:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO liked_tracks (user_id, track_id, payload, starred_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    entry["user_id"],
                    entry["track_id"],
                    json.dumps(entry["payload"], ensure_ascii=False),
                    entry["starred_at"],
                ),
            )
            existing_stars.add(key)
            starred_inserted += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(
        f"imported {len(users)} users, {len(imported_playlists)} playlists, "
        f"{len(legacy_downloads)} download rows, {len(download_playlists)} downloaded playlists, "
        f"{history_inserted} history rows, {starred_inserted} starred tracks"
    )
    print(f"resolution: {resolver.stats}")
    print(f"report: {report_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-db", required=True, help="old hify cache/library.db")
    parser.add_argument(
        "--new-db", default="data/music.db", help="new music.db (default: data/music.db)"
    )
    parser.add_argument("--snapshot", help="snapshot path for the old database")
    parser.add_argument("--cache", help="track resolution cache JSON")
    parser.add_argument("--report", help="report JSON path")
    parser.add_argument("--dry-run", action="store_true", help="resolve and report, write nothing")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
