from __future__ import annotations

import re
from collections import OrderedDict
from itertools import islice
from threading import Lock
from typing import Any

_MAX = 4096
_songs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_song_lock = Lock()
_saved_songs: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()
_SAVED_TRACK_ID = re.compile(r"st-[A-Za-z0-9]{22}|ytv?-[A-Za-z0-9_-]{11}")
_TEXT_FIELDS = (
    "id", "title", "artist", "artistId", "displayArtist", "album", "albumId", "coverArt",
    "suffix", "contentType", "type",
)


def remember(song: dict[str, Any]) -> None:
    song_id = song.get("id")
    if not song_id:
        return
    with _song_lock:
        _songs[song_id] = song
        _songs.move_to_end(song_id)
        while len(_songs) > _MAX:
            _songs.popitem(last=False)


def get_song(song_id: str) -> dict[str, Any] | None:
    with _song_lock:
        song = _songs.get(song_id)
        if song is not None:
            _songs.move_to_end(song_id)
        return song


def library_song(song: Any) -> dict[str, Any] | None:
    """Catalog fields from a stored song, without ratings, timestamps or private extras."""
    if not isinstance(song, dict):
        return None
    song_id = song.get("id")
    if not isinstance(song_id, str) or not _SAVED_TRACK_ID.fullmatch(song_id) or song.get("type") != "music":
        return None
    if not isinstance(song.get("title"), str) or not isinstance(song.get("artist"), str):
        return None
    clean = {key: song[key] for key in _TEXT_FIELDS if isinstance(song.get(key), str)}
    for key in ("duration", "track", "discNumber", "year"):
        if type(song.get(key)) is int and song[key] > 0:
            clean[key] = song[key]
    if type(song.get("isDir")) is bool:
        clean["isDir"] = song["isDir"]
    if isinstance(song.get("artists"), list):
        clean["artists"] = [
            {key: entry[key] for key in ("id", "name") if isinstance(entry.get(key), str)}
            for entry in song["artists"] if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        ]
    if isinstance(song.get("isrc"), list):
        clean["isrc"] = [value for value in song["isrc"] if isinstance(value, str)]
    return clean


def remember_library_songs(user_id: str, songs: list[dict[str, Any]]) -> None:
    """Reuse the owner's stored catalog snapshots when they open their library."""
    if not isinstance(songs, list):
        return
    for song in islice(songs, _MAX):
        clean = library_song(song)
        if not clean or "duration" not in clean:
            continue
        song_id = clean["id"]
        if clean["artist"].strip().lower() in {"", "unknown", "unknown artist"}:
            continue
        if clean["title"].strip().lower() in {
            "", "unknown", "unknown title", "unknown track", "unknown song", "untitled",
            song_id.lower(), song_id.partition("-")[2].lower(),
        }:
            continue
        key = (user_id, song_id)
        _saved_songs[key] = clean
        _saved_songs.move_to_end(key)
        if len(_saved_songs) > _MAX:
            _saved_songs.popitem(last=False)


def get_library_song(song_id: str, user_id: str | None) -> dict[str, Any] | None:
    return get_song(song_id) or (_saved_songs.get((user_id, song_id)) if user_id else None)
