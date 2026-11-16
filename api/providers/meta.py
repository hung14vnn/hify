from __future__ import annotations

from collections import OrderedDict
from typing import Any

_MAX = 4096
_songs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()


def remember(song: dict[str, Any]) -> None:
    song_id = song.get("id")
    if not song_id:
        return
    _songs[song_id] = song
    _songs.move_to_end(song_id)
    while len(_songs) > _MAX:
        _songs.popitem(last=False)


def get_song(song_id: str) -> dict[str, Any] | None:
    song = _songs.get(song_id)
    if song is not None:
        _songs.move_to_end(song_id)
    return song
