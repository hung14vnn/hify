from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import random
import re
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import db
from providers import PlaybackRecording
from auth import current_user
from config import settings
from errors import SubsonicError
from lastfm import LastfmService
from models import album_from_spotify, artist_from_spotify, song_from_spotify, song_from_yt
from providers.flac_bridge import FlacBridge
from providers.meta import remember_library_songs
from providers.spotapi_client import SpotApiClient
from providers.streaming import StreamService
from providers.ytmusic_stream_service import YTMusicStreamService

logger = logging.getLogger(__name__)
router = APIRouter()


def _ok(**payload: Any) -> JSONResponse:
    return JSONResponse(
        {"subsonic-response": {"status": "ok", "version": "1.16.1", **payload}}
    )


def _truthy(value: Any) -> bool:
    return str(value or "").lower() in {"true", "1", "yes"}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _services(request: Request) -> dict[str, Any]:
    return request.app.state.services


@router.get("/ping")
async def ping(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(type="hify", serverVersion="1.0.0", openSubsonic=True)


@router.get("/getUser")
async def get_user(request: Request, user=Depends(current_user)) -> JSONResponse:
    lastfm = await _services(request)["lastfm"].status(user["user_id"])
    return _ok(
        user={
            "username": user["username"],
            "email": user["email"],
            "scrobblingEnabled": bool(lastfm["linked"] and lastfm["scrobbling"]),
            "adminRole": user["role"] == "admin",
            "settingsRole": False,
            "downloadRole": True,
            "uploadRole": False,
            "playlistRole": True,
            "coverArtRole": False,
            "commentRole": False,
            "podcastRole": False,
            "streamRole": True,
            "jukeboxRole": False,
            "shareRole": False,
        }
    )


@router.get("/search3")
async def search3(request: Request, user=Depends(current_user)) -> JSONResponse:
    services = _services(request)
    query = (request.query_params.get("query") or "").strip()
    source = request.query_params.get("source") or "spotify"
    song_count = _int(request.query_params.get("songCount"), 20)
    artist_count = _int(request.query_params.get("artistCount"), 8)
    album_count = _int(request.query_params.get("albumCount"), 10)
    song_offset = _int(request.query_params.get("songOffset"), 0)
    if not query:
        return _ok(searchResult3={})
    if source == "youtube":
        count = max(0, min(song_count, 50))
        offset = max(0, min(song_offset, 200))
        if not count:
            return _ok(searchResult3={"song": []})
        videos = await services["ytmusic"].search_videos(query, limit=count + offset)
        songs = [song_from_yt({
            "videoId": item["id"],
            "title": item["title"],
            "artists": [{"name": item["channel"]}],
            "duration_seconds": item.get("duration"),
            "thumbnails": item.get("thumbnails"),
        }, video=True) for item in videos[offset:offset + count]]
        return _ok(searchResult3={"song": songs})
    if source == "ytmusic":
        tracks = await services["ytmusic"].search_tracks(query, limit=song_count)
        songs = [song_from_yt(item) for item in tracks]
        return _ok(searchResult3={"song": songs})
    spotify: SpotApiClient = services["spotify"]
    limit = max(song_count + song_offset, artist_count, album_count, 10)
    artists, albums, tracks, _ = await spotify.search_all(query, limit=limit)
    if song_offset:
        tracks, _ = await spotify.search_tracks(query, limit=song_count, offset=song_offset)
    return _ok(
        searchResult3={
            "artist": [artist_from_spotify(item) for item in artists[:artist_count]],
            "album": [album_from_spotify(item) for item in albums[:album_count]],
            "song": [song_from_spotify(item) for item in tracks[:song_count]],
        }
    )


@router.get("/searchSpotifyTracks")
async def search_spotify_tracks(request: Request, user=Depends(current_user)) -> JSONResponse:
    services = _services(request)
    query = (request.query_params.get("query") or "").strip()
    count = _int(request.query_params.get("count"), 8)
    if not query:
        return _ok(spotifyTracks={"track": []})
    tracks, _ = await services["spotify"].search_tracks(query, limit=count)
    matches = []
    for track in tracks:
        album = track.get("album") or {}
        images = album.get("images") or []
        matches.append(
            {
                "id": track.get("id"),
                "title": track.get("name"),
                "artist": ", ".join(
                    str(item.get("name")) for item in track.get("artists") or [] if item.get("name")
                ),
                "album": album.get("name") or "",
                "coverUrl": images[0].get("url") if images else None,
                "duration": round((track.get("duration_ms") or 0) / 1000) or None,
            }
        )
    return _ok(spotifyTracks={"track": matches})


@router.get("/stream")
async def stream(request: Request, user=Depends(current_user)):
    services = _services(request)
    song_id = request.query_params.get("id") or ""
    if not song_id:
        raise SubsonicError(70, "Song not found")
    range_header = request.headers.get("range") or request.headers.get("Range")
    user_agent = request.headers.get("user-agent") or request.headers.get("User-Agent")
    format_param = request.query_params.get("format")
    time_offset = (
        request.query_params.get("timeOffset")
        or request.query_params.get("offset")
        or request.query_params.get("time_offset")
    )
    offset_s: float | None = None
    if time_offset:
        try:
            offset_s = max(0.0, float(time_offset))
        except (ValueError, TypeError):
            offset_s = None
    token = request.query_params.get("mixToken") or ""
    recording = None
    if request.query_params.get("autoMix") == "1" and not offset_s and re.fullmatch(r"[\w-]{1,96}", token, re.ASCII):
        recording = PlaybackRecording()
    response = await services["stream"].stream(
        song_id,
        range_header,
        user["user_id"],
        time_offset=offset_s,
        user_agent=user_agent,
        format=format_param,
        recording=recording,
    )
    if recording is not None:
        services["auto_mix"].observe(user["user_id"], song_id, token, recording, response, range_header)
    return response


@router.get("/getAutoMixInfo")
async def get_auto_mix_info(request: Request, user=Depends(current_user)) -> JSONResponse:
    song_id = request.query_params.get("id") or ""
    token = request.query_params.get("mixToken") or ""
    info = await _services(request)["auto_mix"].info(user["user_id"], song_id, token)
    return _ok(autoMixInfo=info)


@router.get("/download")
async def download(request: Request, user=Depends(current_user)):
    services = _services(request)
    song_id = request.query_params.get("id") or ""
    if not song_id:
        raise SubsonicError(70, "Song not found")
    range_header = request.headers.get("range") or request.headers.get("Range")
    user_agent = request.headers.get("user-agent") or request.headers.get("User-Agent")
    format_param = request.query_params.get("format")
    lossless = str(request.query_params.get("lossless") or "").lower() in {"1", "true", "yes"}
    return await services["stream"].download(
        song_id,
        range_header,
        lossless=lossless,
        user_id=user["user_id"],
        user_agent=user_agent,
        format=format_param,
    )


@router.get("/getCoverArt")
async def cover_art(request: Request, user=Depends(current_user)) -> JSONResponse:
    # Every remote song carries a full cover URL in `coverArt`, so the player never
    # reaches this endpoint. Keep it explicit instead of serving local files.
    raise SubsonicError(70, "Cover not found")


@router.get("/getLyrics")
async def get_lyrics(request: Request, user=Depends(current_user)) -> JSONResponse:
    artist = (request.query_params.get("artist") or "").strip()
    title = (request.query_params.get("title") or "").strip()
    if not artist or not title:
        return _ok()
    http: httpx.AsyncClient = _services(request)["http"]
    try:
        response = await http.get(
            "https://lrclib.net/api/get",
            params={"artist_name": artist, "track_name": title},
            timeout=8.0,
        )
        if response.status_code == 200:
            data = response.json()
            value = data.get("plainLyrics") or data.get("lyrics") or ""
            if value:
                return _ok(lyrics={"value": value})
    except (httpx.HTTPError, ValueError):
        pass
    return _ok()


# --- likes ------------------------------------------------------------------


async def _fetch_payload(services: dict[str, Any], kind: str, item_id: str, user_id: str) -> dict[str, Any] | None:
    if kind == "song":
        return await services["stream"].song_meta(item_id, user_id)
    if kind == "artist" and item_id.startswith("sa-"):
        artist = await services["spotify"].get_artist(item_id[3:])
        return artist_from_spotify(artist)
    if kind == "album" and item_id.startswith("sl-"):
        album = await services["spotify"].get_album(item_id[3:])
        return album_from_spotify(album)
    return None


@router.get("/star")
async def star(request: Request, user=Depends(current_user)) -> JSONResponse:
    services = _services(request)
    pairs = [
        ("song", request.query_params.getlist("id")),
        ("artist", request.query_params.getlist("artistId")),
        ("album", request.query_params.getlist("albumId")),
    ]
    for kind, ids in pairs:
        for item_id in ids:
            if not item_id:
                continue
            payload = await _fetch_payload(services, kind, item_id, user["user_id"])
            if payload is not None:
                await db.upsert_star(kind, user["user_id"], item_id, payload)
    return _ok()


@router.get("/unstar")
async def unstar(request: Request, user=Depends(current_user)) -> JSONResponse:
    pairs = [
        ("song", request.query_params.getlist("id")),
        ("artist", request.query_params.getlist("artistId")),
        ("album", request.query_params.getlist("albumId")),
    ]
    for kind, ids in pairs:
        for item_id in ids:
            if item_id:
                await db.delete_star(kind, user["user_id"], item_id)
    return _ok()


@router.get("/getStarred2")
async def get_starred2(request: Request, user=Depends(current_user)) -> JSONResponse:
    uid = user["user_id"]
    songs = await db.list_stars("song", uid)
    remember_library_songs(uid, songs)
    return _ok(
        starred2={
            "artist": await db.list_stars("artist", uid),
            "album": await db.list_stars("album", uid),
            "song": songs,
        }
    )


# --- catalog pages ----------------------------------------------------------


@router.get("/getArtists")
async def get_artists(request: Request, user=Depends(current_user)) -> JSONResponse:
    artists = await db.list_stars("artist", user["user_id"])
    buckets: dict[str, list[dict[str, Any]]] = {}
    for artist in artists:
        name = str(artist.get("name") or "")
        letter = name[:1].upper() if name else "#"
        buckets.setdefault(letter, []).append(artist)
    index = [{"name": key, "artist": buckets[key]} for key in sorted(buckets)]
    return _ok(artists={"ignoredArticles": "The El La Los Las Le Les", "index": index})


@router.get("/getArtist")
async def get_artist(request: Request, user=Depends(current_user)) -> JSONResponse:
    services = _services(request)
    artist_id = request.query_params.get("id") or ""
    spotify: SpotApiClient = services["spotify"]
    if not artist_id.startswith("sa-"):
        # The player links YouTube-sourced songs by artist name; resolve that to
        # the Spotify catalog so an artist page still opens.
        results, _ = await spotify.search_artists(artist_id, limit=1)
        if not results:
            raise SubsonicError(70, "Artist not found")
        artist_id = f"sa-{results[0]['id']}"
    artist = await spotify.get_artist(artist_id[3:])
    albums, _, _ = await spotify.get_artist_albums(artist_id[3:], limit=30)
    return _ok(artist=artist_from_spotify(artist, albums=albums))


@router.get("/getArtistInfo2")
async def get_artist_info(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(artistInfo2={})


@router.get("/getAlbum")
async def get_album(request: Request, user=Depends(current_user)) -> JSONResponse:
    services = _services(request)
    album_id = request.query_params.get("id") or ""
    if not album_id.startswith("sl-"):
        raise SubsonicError(70, "Album not found")
    album = await services["spotify"].get_album(album_id[3:])
    return _ok(album=album_from_spotify(album, with_songs=True))


@router.get("/getAlbumList2")
async def get_album_list2(request: Request, user=Depends(current_user)) -> JSONResponse:
    albums = await db.list_stars("album", user["user_id"])
    kind = request.query_params.get("type") or "starred"
    if kind == "alphabeticalByName":
        albums.sort(key=lambda item: str(item.get("name") or "").lower())
    elif kind == "alphabeticalByArtist":
        albums.sort(key=lambda item: str(item.get("artist") or "").lower())
    return _ok(albumList2={"album": albums})


@router.get("/getTopSongs")
async def get_top_songs(request: Request, user=Depends(current_user)) -> JSONResponse:
    services = _services(request)
    artist = (request.query_params.get("artist") or "").strip()
    count = _int(request.query_params.get("count"), 10)
    if not artist:
        return _ok(topSongs={"song": []})
    tracks, _ = await services["spotify"].search_tracks(artist, limit=max(count * 2, 20))
    matched = [
        song_from_spotify(track)
        for track in tracks
        if any(
            artist.lower() in str(item.get("name") or "").lower()
            for item in track.get("artists") or []
        )
    ]
    return _ok(topSongs={"song": matched[:count]})


# --- home shelves -----------------------------------------------------------


_TODAYS_TOP_HITS = "37i9dQZF1DXcBWIGoYBM5M"
_NEW_MUSIC_FRIDAY = "37i9dQZF1DX4JAvHpjipBk"
_POPULAR_PLAYLISTS = (
    "37i9dQZEVXbMDoHDwVN2tF",
    "37i9dQZEVXbLdGSmz6xilI",
    _TODAYS_TOP_HITS,
    "37i9dQZF1DX4g8Gs5nUhpp",
    "37i9dQZF1DXbYM3nMM0oPk",
    "37i9dQZF1DX0XUsuxWHRQd",
    "37i9dQZF1DWUa8ZRTfalHk",
    "37i9dQZF1DWWQRwui0ExPn",
)
_DISCOVERY_IDS = (*_POPULAR_PLAYLISTS, _NEW_MUSIC_FRIDAY)
_DISCOVERY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DISCOVERY_LOCKS = {playlist_id: asyncio.Lock() for playlist_id in _DISCOVERY_IDS}


async def _discovery_playlist(spotify: SpotApiClient, playlist_id: str) -> dict[str, Any]:
    async with _DISCOVERY_LOCKS[playlist_id]:
        cached = _DISCOVERY_CACHE.get(playlist_id)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        playlist = await spotify.get_playlist(playlist_id)
        if playlist.get("_tracks"):
            _DISCOVERY_CACHE[playlist_id] = (time.monotonic(), playlist)
        return playlist


def _spotify_playlist_json(playlist: dict[str, Any], *, with_songs: bool = False) -> dict[str, Any]:
    tracks = [track for track in playlist.get("_tracks") or [] if track.get("id")]
    images = playlist.get("images") or ((tracks[0].get("album") or {}).get("images") if tracks else []) or []
    result = {
        "id": f"spotify-playlist-{playlist['id']}",
        "name": playlist.get("name") or "Spotify Playlist",
        "owner": "Spotify",
        "public": True,
        "comment": playlist.get("description") or "",
        "songCount": len(tracks),
        "duration": sum(round((track.get("duration_ms") or 0) / 1000) for track in tracks),
        "coverArt": images[0].get("url") if images else None,
    }
    if with_songs:
        result["entry"] = [song_from_spotify(track) for track in tracks]
    return result


async def _chart_songs(services: dict[str, Any], count: int, country: str | None = None) -> list[dict[str, Any]]:
    ytmusic: YTMusicStreamService = services["ytmusic"]
    try:
        tracks = await ytmusic.get_chart_songs(country or settings.yt_country, limit=count)
    except Exception as exc:  # noqa: BLE001 - a dead chart never breaks Home
        logger.warning("chart songs failed: %s", exc)
        return []
    return [song_from_yt(item) for item in tracks]


@router.get("/getTodaysHits")
async def todays_hits(request: Request, user=Depends(current_user)) -> JSONResponse:
    count = max(1, min(_int(request.query_params.get("count"), 20), 100))
    try:
        playlist = await _discovery_playlist(_services(request)["spotify"], _TODAYS_TOP_HITS)
        songs = [song_from_spotify(track) for track in playlist.get("_tracks") or [] if track.get("id")]
    except Exception as exc:
        logger.warning("Today's Top Hits failed: %s", exc)
        songs = []
    return _ok(todaysHits={"song": songs[:count]})


@router.get("/getTrendingSongs")
async def trending_songs(request: Request, user=Depends(current_user)) -> JSONResponse:
    count = max(1, min(_int(request.query_params.get("count"), 20), 100))
    country = (request.query_params.get("country") or settings.yt_country).strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        country = settings.yt_country
    return _ok(trendingSongs={"song": await _chart_songs(_services(request), count, country)})


@router.get("/getRandomSongs")
async def random_songs(request: Request, user=Depends(current_user)) -> JSONResponse:
    size = _int(request.query_params.get("size"), 50)
    songs = await _chart_songs(_services(request), max(size, 20))
    random.shuffle(songs)
    return _ok(randomSongs={"song": songs[:size]})


@router.get("/getRecentlyPlayedSongs")
async def recently_played(request: Request, user=Depends(current_user)) -> JSONResponse:
    count = _int(request.query_params.get("count"), 30)
    offset = _int(request.query_params.get("offset"), 0)
    songs = await db.history_list(user["user_id"], count, offset)
    remember_library_songs(user["user_id"], songs)
    return _ok(recentlyPlayedSongs={"song": songs})


def _watch_track(track: dict[str, Any]) -> dict[str, Any]:
    """A radio (watch playlist) track in the shape `song_from_yt` reads: it carries
    `length` ("3:46") and `thumbnail` instead of a duration and `thumbnails`."""
    out = dict(track)
    if "thumbnails" not in out and out.get("thumbnail"):
        out["thumbnails"] = out["thumbnail"]
    length = str(out.get("length") or "")
    if not out.get("duration_seconds") and re.fullmatch(r"\d+(:\d{1,2}){1,2}", length):
        seconds = 0
        for part in length.split(":"):
            seconds = seconds * 60 + int(part)
        out["duration_seconds"] = seconds
    return out


async def _seed_video_id(
    services: dict[str, Any], song_id: str = "", artist: str = "", title: str = ""
) -> str | None:
    """The YouTube Music video a seed track maps to (radio mixes are keyed by video)."""
    if song_id.startswith("yt-"):
        return song_id[3:] or None
    if song_id.startswith("ytv-"):
        return song_id[4:] or None
    if song_id:
        meta = await services["stream"].song_meta(song_id)
        if meta:
            artist = str(meta.get("artist") or "")
            title = str(meta.get("title") or "")
    if not title:
        return None
    ytmusic: YTMusicStreamService = services["ytmusic"]
    results = await ytmusic.search_tracks(f"{artist} {title}".strip(), limit=1)
    return str(results[0]["videoId"]) if results else None


@router.get("/getSimilarSongs2")
async def similar_songs(request: Request, user=Depends(current_user)) -> JSONResponse:
    """Songs like the seeds, from YouTube Music radio mixes.

    Two callers: Create Station sends one `id`; Smart Discover (queue suggestions)
    sends up to five `artist`/`title` pairs, repeated in order.
    """
    services = _services(request)
    count = max(1, min(_int(request.query_params.get("count"), 50), 100))
    params = request.query_params
    seeds = [{"song_id": song_id} for song_id in params.getlist("id") if song_id]
    seeds += [
        {"artist": artist, "title": title}
        for artist, title in zip(params.getlist("artist"), params.getlist("title"))
        if title
    ]
    seeds = seeds[:5]
    if not seeds:
        return _ok(similarSongs2={"song": []})
    resolved = await asyncio.gather(
        *(_seed_video_id(services, **seed) for seed in seeds), return_exceptions=True
    )
    video_ids = [vid for vid in resolved if isinstance(vid, str) and vid]
    for seed, result in zip(seeds, resolved):
        if isinstance(result, Exception):
            logger.warning("similar songs: seed %s failed: %s", seed, result)
    if not video_ids:
        return _ok(similarSongs2={"song": []})
    ytmusic: YTMusicStreamService = services["ytmusic"]
    try:
        tracks = await ytmusic.get_smart_discover(video_ids, limit=count)
    except Exception as exc:  # noqa: BLE001 - no suggestions beats an error toast
        logger.warning("similar songs failed: %s", exc)
        tracks = []
    return _ok(similarSongs2={"song": [song_from_yt(_watch_track(track)) for track in tracks]})


@router.get("/getGenres")
async def get_genres(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(genres={"genre": []})


@router.get("/getSongsByGenre")
async def songs_by_genre(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(songsByGenre={"song": []})


@router.get("/getTrendingPlaylists")
async def trending_playlists(request: Request, user=Depends(current_user)) -> JSONResponse:
    spotify = _services(request)["spotify"]

    async def fetch(playlist_id: str) -> dict[str, Any] | None:
        try:
            return _spotify_playlist_json(await _discovery_playlist(spotify, playlist_id))
        except Exception as exc:
            logger.warning("Popular playlist %s failed: %s", playlist_id, exc)
            return None

    playlists = await asyncio.gather(*(fetch(playlist_id) for playlist_id in _POPULAR_PLAYLISTS))
    return _ok(playlists={"playlist": [playlist for playlist in playlists if playlist]})


@router.get("/getYtMusicNewReleases")
async def yt_new_releases(request: Request, user=Depends(current_user)) -> JSONResponse:
    count = max(1, min(_int(request.query_params.get("count"), 20), 100))
    try:
        playlist = await _discovery_playlist(_services(request)["spotify"], _NEW_MUSIC_FRIDAY)
    except Exception as exc:
        logger.warning("New Music Friday failed: %s", exc)
        return _ok(albumList2={"album": []})
    albums = {}
    for track in playlist.get("_tracks") or []:
        album = track.get("album") or {}
        album_id = album.get("id")
        if album_id and album_id not in albums:
            albums[album_id] = album_from_spotify(album)
        if len(albums) >= count:
            break
    return _ok(albumList2={"album": list(albums.values())})


@router.get("/getRandomRadioMix")
async def random_radio_mix(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(playlists={"playlist": []})


# --- playlists --------------------------------------------------------------


def _playlist_json(row: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "comment": row.get("comment") or "",
        "owner": row.get("owner") or "",
        "public": bool(row.get("public", 1)),
        "songCount": len(entries),
        "duration": sum(int(item.get("duration") or 0) for item in entries),
        "coverArt": entries[0].get("coverArt") if entries else None,
        "created": dt.datetime.fromtimestamp(row["created_at"], dt.UTC).isoformat(),
        "changed": dt.datetime.fromtimestamp(row["updated_at"], dt.UTC).isoformat(),
        "entry": entries,
    }


@router.get("/getPlaylists")
async def get_playlists(request: Request, user=Depends(current_user)) -> JSONResponse:
    rows = await db.playlists_for(user["user_id"])
    # Each card has a Play button for its first song. Detail loads the selected list.
    for row in rows:
        remember_library_songs(user["user_id"], row["entry"][:1])
    playlists = [_playlist_json(row, row["entry"]) for row in rows]
    return _ok(playlists={"playlist": playlists})


@router.get("/getPlaylist")
async def get_playlist(request: Request, user=Depends(current_user)) -> JSONResponse:
    playlist_id = request.query_params.get("id") or ""
    if playlist_id.startswith("spotify-playlist-"):
        spotify_id = playlist_id.removeprefix("spotify-playlist-")
        if not re.fullmatch(r"[A-Za-z0-9]{22}", spotify_id):
            raise SubsonicError(70, "Playlist not found")
        spotify = _services(request)["spotify"]
        try:
            if spotify_id in _DISCOVERY_IDS:
                playlist = await _discovery_playlist(spotify, spotify_id)
            else:
                playlist = await spotify.get_playlist(spotify_id)
        except Exception as exc:
            logger.warning("Spotify playlist %s failed: %s", spotify_id, exc)
            raise SubsonicError(70, "Spotify playlist is unavailable. Try again later.") from exc
        return _ok(playlist=_spotify_playlist_json(playlist, with_songs=True))
    full = await db.playlist(playlist_id, user["user_id"])
    if not full:
        raise SubsonicError(70, "Playlist not found")
    remember_library_songs(user["user_id"], full["entry"])
    return _ok(playlist=_playlist_json(full, full.get("entry") or []))


@router.get("/createPlaylist")
async def create_playlist(request: Request, user=Depends(current_user)) -> JSONResponse:
    name = (request.query_params.get("name") or "").strip()
    song_ids = request.query_params.getlist("songId")
    if not name:
        raise SubsonicError(10, "Playlist name required")
    playlist_id = await db.create_playlist(user, name, song_ids)
    full = await db.playlist(playlist_id, user["user_id"])
    return _ok(playlist=_playlist_json(full or {}, full.get("entry") or []))


@router.get("/updatePlaylist")
async def update_playlist(request: Request, user=Depends(current_user)) -> JSONResponse:
    playlist_id = request.query_params.get("playlistId") or ""
    name = request.query_params.get("name")
    comment = request.query_params.get("comment")
    public_param = request.query_params.get("public")
    add_ids = request.query_params.getlist("songIdToAdd")
    remove_indexes = [
        _int(value)
        for value in request.query_params.getlist("songIndexToRemove")
        if str(value).strip().lstrip("-").isdigit()
    ]
    await db.update_playlist(
        playlist_id,
        user["user_id"],
        name=name,
        comment=comment,
        public=_truthy(public_param) if public_param is not None else None,
        add_ids=[item for item in add_ids if item],
        remove_indexes=remove_indexes,
    )
    return _ok()


@router.get("/deletePlaylist")
async def delete_playlist(request: Request, user=Depends(current_user)) -> JSONResponse:
    await db.delete_playlist(request.query_params.get("id") or "", user["user_id"])
    return _ok()


# --- queue / scrobble -------------------------------------------------------


@router.get("/getPlayQueue")
async def get_play_queue(request: Request, user=Depends(current_user)) -> JSONResponse:
    queue = await db.get_queue(user["user_id"])
    if not queue:
        return _ok()
    remember_library_songs(user["user_id"], queue["entry"])
    return _ok(
        playQueue={
            "entry": queue["entry"],
            "current": queue["current"],
            "position": queue["position"],
        }
    )


@router.get("/savePlayQueue")
async def save_play_queue(request: Request, user=Depends(current_user)) -> JSONResponse:
    song_ids = request.query_params.getlist("id")
    current = request.query_params.get("current")
    position = _float(request.query_params.get("position"), 0.0)
    await db.save_queue(user["user_id"], song_ids, current, position)
    return _ok()


@router.get("/scrobble")
async def scrobble(request: Request, user=Depends(current_user)) -> JSONResponse:
    services = _services(request)
    song_id = request.query_params.get("id") or ""
    submission = _truthy(request.query_params.get("submission"))
    timestamp = _int(request.query_params.get("time"), int(time.time() * 1000)) // 1000
    song = await services["stream"].song_meta(song_id, user["user_id"])
    if song:
        artist = str(song.get("displayArtist") or song.get("artist") or "")
        title = str(song.get("title") or "")
        album = str(song.get("album") or "")
        duration = int(song.get("duration") or 0) or None
        lastfm: LastfmService = services["lastfm"]
        if submission:
            await db.history_add(user["user_id"], song)
            await lastfm.scrobble(user["user_id"], artist, title, album, duration, timestamp)
        else:
            await lastfm.now_playing(user["user_id"], artist, title, album, duration)
    return _ok()


# --- Last.fm (Subsonic shims used by the player's Profile page) -------------


@router.get("/getLastfmStatus")
async def get_lastfm_status(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(lastfm=await _services(request)["lastfm"].status(user["user_id"]))


@router.get("/setLastfmScrobbling")
async def set_lastfm_scrobbling(request: Request, user=Depends(current_user)) -> JSONResponse:
    enabled = _truthy(request.query_params.get("enabled"))
    status = await _services(request)["lastfm"].set_scrobbling(user["user_id"], enabled)
    return _ok(lastfm=status)


_RECS_TTL_S = 30 * 60
_RECS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


async def _recommended_songs(services: dict[str, Any], lastfm: LastfmService, username: str) -> list[dict[str, Any]]:
    """A radio blend seeded by tracks the user actually plays a lot lately."""
    tracks = await lastfm.top_tracks(username, period="1month", limit=30)
    if len(tracks) < 5:
        tracks += await lastfm.top_tracks(username, period="overall", limit=30)
    if not tracks:
        return []
    pool = list(dict.fromkeys(tracks))[:15]
    seeds = random.sample(pool, min(5, len(pool)))
    resolved = await asyncio.gather(
        *(_seed_video_id(services, artist=artist, title=title) for artist, title in seeds),
        return_exceptions=True,
    )
    video_ids = [vid for vid in resolved if isinstance(vid, str) and vid]
    if not video_ids:
        return []
    ytmusic: YTMusicStreamService = services["ytmusic"]
    radio = await ytmusic.get_smart_discover(video_ids, limit=25)
    played = {(a.lower(), t.lower()) for a, t in tracks}
    songs = [song_from_yt(_watch_track(track)) for track in radio]
    # Recommendations, not a replay of the user's own top list.
    return [s for s in songs if (str(s.get("artist", "")).lower(), str(s.get("title", "")).lower()) not in played]


async def _recommended_artists(services: dict[str, Any], lastfm: LastfmService, username: str) -> list[dict[str, Any]]:
    """Artists similar to the user's favourites that they don't already listen to."""
    top = await lastfm.top_artists(username, period="3month", limit=50)
    if len(top) < 5:
        top += await lastfm.top_artists(username, period="overall", limit=50)
    if not top:
        return []
    known = {name.lower() for name in top}
    seeds = list(dict.fromkeys(top))[:5]
    similar = await asyncio.gather(*(lastfm.similar_artists(seed, limit=12) for seed in seeds))
    picks: list[tuple[str, str]] = []
    seen: set[str] = set()
    # Round-robin across seeds so one favourite doesn't fill the whole shelf.
    for i in range(12):
        for seed, names in zip(seeds, similar):
            if i < len(names) and names[i].lower() not in known and names[i].lower() not in seen:
                seen.add(names[i].lower())
                picks.append((names[i], seed))
    picks = picks[:12]
    spotify: SpotApiClient = services["spotify"]

    async def card(name: str, seed: str) -> dict[str, Any]:
        item: dict[str, Any] = {"id": name, "name": name}  # getArtist also resolves a plain name
        try:
            results, _ = await spotify.search_artists(name, limit=1)
            if results:
                item = artist_from_spotify(results[0])
        except Exception as exc:  # noqa: BLE001 - a card without a picture is still a pick
            logger.debug("spotify artist lookup failed for %s: %s", name, exc)
        item["reason"] = f"Similar to {seed}"
        return item

    return list(await asyncio.gather(*(card(name, seed) for name, seed in picks)))


async def _recommended_albums(services: dict[str, Any], artists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One album from each recommended artist (a full album over singles/EPs)."""
    spotify: SpotApiClient = services["spotify"]

    async def pick(artist: dict[str, Any]) -> dict[str, Any] | None:
        artist_id = str(artist.get("id") or "")
        if not artist_id.startswith("sa-"):
            return None  # no catalog match: nothing to open
        albums, _, _ = await spotify.get_artist_albums(artist_id[3:], limit=10)
        chosen = next((a for a in albums if a.get("album_type") == "album"), albums[0] if albums else None)
        if not chosen:
            return None
        item = album_from_spotify(chosen)
        if artist.get("reason"):
            item["reason"] = artist["reason"]
        return item

    results = await asyncio.gather(*(pick(artist) for artist in artists[:10]), return_exceptions=True)
    return [album for album in results if isinstance(album, dict)]


@router.get("/getLastfmRecommendations")
async def get_lastfm_recommendations(request: Request, user=Depends(current_user)) -> JSONResponse:
    """Home's "For You" shelves from the linked Last.fm account (empty when not linked)."""
    services = _services(request)
    lastfm: LastfmService = services["lastfm"]
    uid = user["user_id"]
    username = await lastfm.linked_username(uid)
    if not username:
        return _ok(lastfmRecommendations={"shelf": []})
    refresh = str(request.query_params.get("refresh") or "").lower() in {"1", "true", "yes"}
    cached = _RECS_CACHE.get(uid)
    if cached and not refresh and time.monotonic() - cached[0] < _RECS_TTL_S:
        return _ok(lastfmRecommendations={"shelf": cached[1]})

    songs, artists = await asyncio.gather(
        _recommended_songs(services, lastfm, username),
        _recommended_artists(services, lastfm, username),
        return_exceptions=True,
    )
    albums: list[dict[str, Any]] | BaseException = []
    if isinstance(artists, list) and artists:
        try:
            albums = await _recommended_albums(services, artists)
        except Exception as exc:  # noqa: BLE001 - the other shelves still show
            albums = exc
    shelves: list[dict[str, Any]] = []
    if isinstance(songs, list) and songs:
        shelves.append({
            "key": "lastfm-songs",
            "kind": "song",
            "title": "Made for You",
            "subtitle": "Based on what you play on Last.fm",
            "song": songs,
        })
    if isinstance(artists, list) and artists:
        shelves.append({
            "key": "lastfm-artists",
            "kind": "artist",
            "title": "Artists You Might Like",
            "artist": artists,
        })
    if isinstance(albums, list) and albums:
        shelves.append({
            "key": "lastfm-albums",
            "kind": "album",
            "title": "Albums You Might Like",
            "album": albums,
        })
    for label, result in (("songs", songs), ("artists", artists), ("albums", albums)):
        if isinstance(result, Exception):
            logger.warning("lastfm recommendations (%s) failed: %s", label, result)
    # Only keep a full answer: a partial or empty one is retried on the next visit.
    if len(shelves) == 3:
        _RECS_CACHE[uid] = (time.monotonic(), shelves)
    return _ok(lastfmRecommendations={"shelf": shelves})


# --- library admin (removed in the slim app) --------------------------------


@router.get("/getScanStatus")
async def scan_status(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(scanStatus={"scanning": False, "count": 0})


@router.get("/startScan")
async def start_scan(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(scanStatus={"scanning": False, "count": 0})
