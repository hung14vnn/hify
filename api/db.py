from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

from config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    username   TEXT NOT NULL,
    email      TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'user',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS liked_tracks (
    user_id    TEXT NOT NULL,
    track_id   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    starred_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, track_id)
);

CREATE TABLE IF NOT EXISTS saved_artists (
    user_id    TEXT NOT NULL,
    artist_id  TEXT NOT NULL,
    payload    TEXT NOT NULL,
    starred_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, artist_id)
);

CREATE TABLE IF NOT EXISTS saved_albums (
    user_id    TEXT NOT NULL,
    album_id   TEXT NOT NULL,
    payload    TEXT NOT NULL,
    starred_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, album_id)
);

CREATE TABLE IF NOT EXISTS playlists (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    comment    TEXT NOT NULL DEFAULT '',
    owner      TEXT NOT NULL DEFAULT '',
    public     INTEGER NOT NULL DEFAULT 1,
    cover      TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playlists_user ON playlists(user_id);

CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id TEXT NOT NULL,
    position    INTEGER NOT NULL,
    track_id    TEXT NOT NULL,
    payload     TEXT NOT NULL,
    PRIMARY KEY (playlist_id, position)
);

CREATE TABLE IF NOT EXISTS play_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   TEXT NOT NULL,
    track_id  TEXT NOT NULL,
    payload   TEXT NOT NULL,
    played_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_user ON play_history(user_id, played_at DESC);

CREATE TABLE IF NOT EXISTS play_queue (
    user_id    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    current    TEXT,
    position   REAL NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS lastfm_connections (
    user_id     TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    username    TEXT NOT NULL DEFAULT '',
    scrobbling  INTEGER NOT NULL DEFAULT 1,
    connected_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_prefs (
    user_id TEXT PRIMARY KEY,
    quality TEXT NOT NULL DEFAULT 'HI_RES_LOSSLESS'
);
"""


def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or settings.music_db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    settings.music_db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


async def _run(fn):  # type: ignore[no-untyped-def]
    return await asyncio.to_thread(fn)


# --- sessions ---------------------------------------------------------------


def _create_session(
    token_hash: str, user: dict[str, Any], ttl_days: int
) -> None:
    now = int(time.time())
    expires = now + ttl_days * 86400
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions"
            " (token_hash, user_id, username, email, role, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token_hash, user["id"], user["name"], user["email"], user["role"], now, expires),
        )
        conn.commit()


async def create_session(token_hash: str, user: dict[str, Any]) -> None:
    await _run(lambda: _create_session(token_hash, user, settings.session_ttl_days))


def _get_session(token_hash: str) -> dict[str, Any] | None:
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (token_hash, now),
        ).fetchone()
        return dict(row) if row else None


async def get_session(token_hash: str) -> dict[str, Any] | None:
    return await _run(lambda: _get_session(token_hash))


def _delete_session(token_hash: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        conn.commit()


async def delete_session(token_hash: str) -> None:
    await _run(lambda: _delete_session(token_hash))


# --- saved items ------------------------------------------------------------


_STAR_TABLES = {
    "song": ("liked_tracks", "track_id"),
    "artist": ("saved_artists", "artist_id"),
    "album": ("saved_albums", "album_id"),
}


def _upsert_star(kind: str, user_id: str, item_id: str, payload: dict[str, Any]) -> None:
    table, column = _STAR_TABLES[kind]
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {table} (user_id, {column}, payload, starred_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, item_id, json.dumps(payload, ensure_ascii=False), now),
        )
        conn.commit()


async def upsert_star(kind: str, user_id: str, item_id: str, payload: dict[str, Any]) -> None:
    await _run(lambda: _upsert_star(kind, user_id, item_id, payload))


def _delete_star(kind: str, user_id: str, item_id: str) -> None:
    table, column = _STAR_TABLES[kind]
    with _connect() as conn:
        conn.execute(
            f"DELETE FROM {table} WHERE user_id = ? AND {column} = ?", (user_id, item_id)
        )
        conn.commit()


async def delete_star(kind: str, user_id: str, item_id: str) -> None:
    await _run(lambda: _delete_star(kind, user_id, item_id))


def _list_stars(kind: str, user_id: str) -> list[dict[str, Any]]:
    table, column = _STAR_TABLES[kind]
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {column} AS item_id, payload, starred_at FROM {table}"
            " WHERE user_id = ? ORDER BY starred_at DESC",
            (user_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = json.loads(row["payload"])
        item["starred"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["starred_at"]))
        result.append(item)
    return result


async def list_stars(kind: str, user_id: str) -> list[dict[str, Any]]:
    return await _run(lambda: _list_stars(kind, user_id))


# --- playlists --------------------------------------------------------------


def _playlist_row(conn: sqlite3.Connection, playlist_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    return dict(row) if row else None


def _create_playlist(user: dict[str, Any], name: str, song_ids: list[str]) -> str:
    playlist_id = "pl-" + secrets.token_hex(8)
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO playlists (id, user_id, name, comment, owner, public, created_at, updated_at)"
            " VALUES (?, ?, ?, '', ?, 1, ?, ?)",
            (playlist_id, user["user_id"], name, user["username"], now, now),
        )
        conn.commit()
    if song_ids:
        _append_items(playlist_id, song_ids)
    return playlist_id


def _append_items(playlist_id: str, song_ids: list[str], payloads: dict[str, dict] | None = None) -> None:
    from providers.meta import get_song, remember

    payloads = payloads or {}
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS p FROM playlist_items WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        position = int(row["p"] or -1) + 1
        for song_id in song_ids:
            payload = payloads.get(song_id) or get_song(song_id)
            if not payload:
                continue
            remember(payload)
            conn.execute(
                "INSERT OR REPLACE INTO playlist_items (playlist_id, position, track_id, payload)"
                " VALUES (?, ?, ?, ?)",
                (playlist_id, position, song_id, json.dumps(payload, ensure_ascii=False)),
            )
            position += 1
        conn.execute(
            "UPDATE playlists SET updated_at = ? WHERE id = ?", (int(time.time()), playlist_id)
        )
        conn.commit()


async def create_playlist(user: dict[str, Any], name: str, song_ids: list[str]) -> str:
    return await _run(lambda: _create_playlist(user, name, song_ids))


def _playlists_for(user_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM playlists WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)
        ).fetchall()
    return [dict(row) for row in rows]


async def playlists_for(user_id: str) -> list[dict[str, Any]]:
    return await _run(lambda: _playlists_for(user_id))


def _playlist_items(playlist_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM playlist_items WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _playlist(playlist_id: str, user_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = _playlist_row(conn, playlist_id)
    if row is None or row["user_id"] != user_id:
        return None
    row["entry"] = _playlist_items(playlist_id)
    return row


async def playlist(playlist_id: str, user_id: str) -> dict[str, Any] | None:
    return await _run(lambda: _playlist(playlist_id, user_id))


def _update_playlist(
    playlist_id: str,
    user_id: str,
    *,
    name: str | None,
    comment: str | None,
    public: bool | None,
    add_ids: list[str],
    remove_indexes: list[int],
) -> None:
    from providers.meta import get_song

    with _connect() as conn:
        row = _playlist_row(conn, playlist_id)
        if row is None or row["user_id"] != user_id:
            return
        items = [
            dict(item)
            for item in conn.execute(
                "SELECT track_id, payload FROM playlist_items WHERE playlist_id = ? ORDER BY position",
                (playlist_id,),
            ).fetchall()
        ]
        for index in sorted(set(remove_indexes), reverse=True):
            if 0 <= index < len(items):
                items.pop(index)
        for song_id in add_ids:
            payload = get_song(song_id)
            if payload:
                items.append({"track_id": song_id, "payload": json.dumps(payload, ensure_ascii=False)})
        conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
        for position, item in enumerate(items):
            conn.execute(
                "INSERT INTO playlist_items (playlist_id, position, track_id, payload)"
                " VALUES (?, ?, ?, ?)",
                (playlist_id, position, item["track_id"], item["payload"]),
            )
        sets = ["updated_at = ?"]
        values: list[Any] = [int(time.time())]
        if name is not None:
            sets.append("name = ?")
            values.append(name)
        if comment is not None:
            sets.append("comment = ?")
            values.append(comment)
        if public is not None:
            sets.append("public = ?")
            values.append(1 if public else 0)
        values.append(playlist_id)
        conn.execute(f"UPDATE playlists SET {', '.join(sets)} WHERE id = ?", values)
        conn.commit()


async def update_playlist(
    playlist_id: str,
    user_id: str,
    *,
    name: str | None = None,
    comment: str | None = None,
    public: bool | None = None,
    add_ids: list[str] | None = None,
    remove_indexes: list[int] | None = None,
) -> None:
    await _run(
        lambda: _update_playlist(
            playlist_id,
            user_id,
            name=name,
            comment=comment,
            public=public,
            add_ids=add_ids or [],
            remove_indexes=remove_indexes or [],
        )
    )


def _delete_playlist(playlist_id: str, user_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
        conn.execute(
            "DELETE FROM playlists WHERE id = ? AND user_id = ?", (playlist_id, user_id)
        )
        conn.commit()


async def delete_playlist(playlist_id: str, user_id: str) -> None:
    await _run(lambda: _delete_playlist(playlist_id, user_id))


# --- history / queue --------------------------------------------------------


def _history_add(user_id: str, payload: dict[str, Any]) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO play_history (user_id, track_id, payload, played_at) VALUES (?, ?, ?, ?)",
            (user_id, payload.get("id", ""), json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
        conn.commit()


async def history_add(user_id: str, payload: dict[str, Any]) -> None:
    await _run(lambda: _history_add(user_id, payload))


def _history_list(user_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT payload, played_at FROM play_history WHERE user_id = ?"
            " ORDER BY played_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
    result = []
    seen: set[str] = set()
    for row in rows:
        payload = json.loads(row["payload"])
        track_id = payload.get("id", "")
        if track_id in seen:
            continue
        seen.add(track_id)
        payload["played"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["played_at"]))
        result.append(payload)
    return result


async def history_list(user_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
    return await _run(lambda: _history_list(user_id, limit, offset))


def _save_queue(user_id: str, songs: list[dict[str, Any]], current: str | None, position: float) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO play_queue (user_id, payload, current, position, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                user_id,
                json.dumps(songs, ensure_ascii=False),
                current,
                position,
                int(time.time()),
            ),
        )
        conn.commit()


async def save_queue(user_id: str, songs: list[dict[str, Any]], current: str | None, position: float) -> None:
    await _run(lambda: _save_queue(user_id, songs, current, position))


def _get_queue(user_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM play_queue WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    return {
        "entry": json.loads(row["payload"]),
        "current": row["current"],
        "position": float(row["position"] or 0),
    }


async def get_queue(user_id: str) -> dict[str, Any] | None:
    return await _run(lambda: _get_queue(user_id))


# --- Last.fm ----------------------------------------------------------------


def _lastfm_get(user_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM lastfm_connections WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


async def lastfm_get(user_id: str) -> dict[str, Any] | None:
    return await _run(lambda: _lastfm_get(user_id))


def _lastfm_save(user_id: str, session_key: str, username: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO lastfm_connections"
            " (user_id, session_key, username, scrobbling, connected_at)"
            " VALUES (?, ?, ?, 1, ?)",
            (user_id, session_key, username, int(time.time())),
        )
        conn.commit()


async def lastfm_save(user_id: str, session_key: str, username: str) -> None:
    await _run(lambda: _lastfm_save(user_id, session_key, username))


def _lastfm_toggle(user_id: str, enabled: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE lastfm_connections SET scrobbling = ? WHERE user_id = ?",
            (1 if enabled else 0, user_id),
        )
        conn.commit()


async def lastfm_toggle(user_id: str, enabled: bool) -> None:
    await _run(lambda: _lastfm_toggle(user_id, enabled))


def _lastfm_delete(user_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM lastfm_connections WHERE user_id = ?", (user_id,))
        conn.commit()


async def lastfm_delete(user_id: str) -> None:
    await _run(lambda: _lastfm_delete(user_id))


def _lastfm_usernames() -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT username FROM lastfm_connections").fetchall()
    return {row["username"] for row in rows if row["username"]}


async def lastfm_usernames() -> set[str]:
    return await _run(_lastfm_usernames)


# --- playback quality -------------------------------------------------------

DEFAULT_QUALITY = "HI_RES_LOSSLESS"
QUALITY_VALUES = ("HI_RES_LOSSLESS", "LOSSLESS", "HIGH")


def _get_quality(user_id: str) -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT quality FROM user_prefs WHERE user_id = ?", (user_id,)
        ).fetchone()
    value = str(row["quality"]) if row else DEFAULT_QUALITY
    return value if value in QUALITY_VALUES else DEFAULT_QUALITY


async def get_quality(user_id: str) -> str:
    return await _run(lambda: _get_quality(user_id))


def _set_quality(user_id: str, quality: str) -> str:
    if quality not in QUALITY_VALUES:
        quality = DEFAULT_QUALITY
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_prefs (user_id, quality) VALUES (?, ?)",
            (user_id, quality),
        )
        conn.commit()
    return quality


async def set_quality(user_id: str, quality: str) -> str:
    return await _run(lambda: _set_quality(user_id, quality))
