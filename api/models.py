from __future__ import annotations

import re
from typing import Any

from providers.meta import remember


def _image(images: Any) -> str | None:
    if not isinstance(images, list):
        return None
    for item in images:
        if isinstance(item, dict) and item.get("url"):
            return str(item["url"])
    return None


def _year(release_date: Any) -> int | None:
    text = str(release_date or "")
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def _artist_names(artists: Any) -> list[str]:
    if not isinstance(artists, list):
        return []
    return [str(item.get("name")) for item in artists if isinstance(item, dict) and item.get("name")]


def song_from_spotify(track: dict[str, Any], *, album: dict[str, Any] | None = None) -> dict[str, Any]:
    album_dict = album if isinstance(album, dict) and album else (track.get("album") if isinstance(track.get("album"), dict) else {})
    artists = track.get("artists") if isinstance(track.get("artists"), list) else []
    names = _artist_names(artists)
    album_id = album_dict.get("id")
    first_artist = artists[0] if artists and isinstance(artists[0], dict) else {}
    isrc = (track.get("external_ids") or {}).get("isrc")
    duration_ms = track.get("duration_ms")
    song = {
        "id": f"st-{track.get('id', '')}",
        "title": str(track.get("name") or ""),
        "album": str(album_dict.get("name") or ""),
        "albumId": f"sl-{album_id}" if album_id else None,
        "artist": ", ".join(names) or "Unknown Artist",
        "artistId": f"sa-{first_artist.get('id')}" if first_artist.get("id") else None,
        "displayArtist": ", ".join(names) or None,
        "artists": [
            {"id": f"sa-{item.get('id')}", "name": str(item.get("name"))}
            for item in artists
            if isinstance(item, dict) and item.get("id")
        ],
        "track": track.get("track_number"),
        "discNumber": track.get("disc_number"),
        "year": _year(album_dict.get("release_date")),
        "coverArt": _image(album_dict.get("images")),
        "duration": int(round(duration_ms / 1000)) if duration_ms else None,
        "type": "music",
        "isDir": False,
        "isrc": [isrc] if isrc else None,
    }
    remember(song)
    return {key: value for key, value in song.items() if value is not None}


def album_from_spotify(album: dict[str, Any], *, with_songs: bool = False) -> dict[str, Any]:
    artists = album.get("artists") if isinstance(album.get("artists"), list) else []
    names = _artist_names(artists)
    first_artist = artists[0] if artists and isinstance(artists[0], dict) else {}
    item = {
        "id": f"sl-{album.get('id', '')}",
        "name": str(album.get("name") or ""),
        "artist": ", ".join(names) or "Unknown Artist",
        "artistId": f"sa-{first_artist.get('id')}" if first_artist.get("id") else None,
        "displayArtist": ", ".join(names) or None,
        "coverArt": _image(album.get("images")),
        "songCount": album.get("total_tracks"),
        "year": _year(album.get("release_date")),
        "created": None,
    }
    if with_songs:
        tracks = (album.get("tracks") or {}).get("items") or []
        item["song"] = [song_from_spotify(track, album=album) for track in tracks if isinstance(track, dict)]
    return {key: value for key, value in item.items() if value is not None}


def artist_from_spotify(artist: dict[str, Any], *, albums: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    image = _image(artist.get("images"))
    item = {
        "id": f"sa-{artist.get('id', '')}",
        "name": str(artist.get("name") or ""),
        "coverArt": image,
        "artistImageUrl": image,
        "albumCount": len(albums) if albums is not None else None,
    }
    if albums is not None:
        item["album"] = [album_from_spotify(album) for album in albums]
    return {key: value for key, value in item.items() if value is not None}


def _upgrade_yt_image_url(url: str) -> str:
    if "googleusercontent.com" in url or "ggpht.com" in url:
        if re.search(r"=w\d+-h\d+", url):
            return re.sub(r"=w\d+-h\d+[^?#]*", "=w800-h800-l90-rj", url)
        if re.search(r"=s\d+", url):
            return re.sub(r"=s\d+[^?#]*", "=s800", url)
        if "=" not in url:
            return f"{url}=w800-h800-l90-rj"
    elif "i.ytimg.com" in url or "img.youtube.com" in url:
        return re.sub(r"/(default|mqdefault)\.jpg", "/hqdefault.jpg", url)
    return url


def _yt_image(thumbnails: Any) -> str | None:
    if isinstance(thumbnails, list) and thumbnails:
        best: str | None = None
        max_dim = 0
        for item in thumbnails:
            if isinstance(item, dict) and item.get("url"):
                w = item.get("width") or 0
                h = item.get("height") or 0
                dim = max(int(w) if isinstance(w, (int, float)) else 0, int(h) if isinstance(h, (int, float)) else 0)
                if dim >= max_dim:
                    max_dim = dim
                    best = str(item["url"])
        if not best:
            last = thumbnails[-1]
            if isinstance(last, dict) and last.get("url"):
                best = str(last["url"])
        if best:
            return _upgrade_yt_image_url(best)
    return None


def song_from_yt(track: dict[str, Any], *, video: bool = False) -> dict[str, Any]:
    video_id = track.get("videoId") or track.get("video_id") or ""
    artists = track.get("artists")
    names: list[str] = []
    if isinstance(artists, list):
        for artist in artists:
            if isinstance(artist, dict) and artist.get("name"):
                names.append(str(artist["name"]))
            elif isinstance(artist, str):
                names.append(artist)
    duration = track.get("duration_seconds") or track.get("duration")
    song = {
        "id": f"{'ytv' if video else 'yt'}-{video_id}",
        "title": str(track.get("title") or ""),
        "album": str((track.get("album") or {}).get("name") or "") if isinstance(track.get("album"), dict) else "",
        "artist": ", ".join(names) or "Unknown Artist",
        "displayArtist": ", ".join(names) or None,
        "coverArt": _yt_image(track.get("thumbnails")),
        "duration": int(duration) if duration else None,
        "suffix": "opus",
        "contentType": "audio/ogg",
        "type": "music",
        "isDir": False,
    }
    remember(song)
    return {key: value for key, value in song.items() if value is not None}
