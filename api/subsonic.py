from __future__ import annotations

import datetime as dt
import hashlib
import logging
import random
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import db
from auth import current_user
from config import settings
from errors import SubsonicError
from lastfm import LastfmService
from models import album_from_spotify, artist_from_spotify, song_from_spotify, song_from_yt
from providers.flac_bridge import FlacBridge
from providers.meta import get_song
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
            "downloadRole": False,
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
    return await services["stream"].stream(
        song_id,
        request.headers.get("range") or request.headers.get("Range"),
        user["user_id"],
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


async def _fetch_payload(services: dict[str, Any], kind: str, item_id: str) -> dict[str, Any] | None:
    if kind == "song":
        return await services["stream"].song_meta(item_id)
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
            payload = await _fetch_payload(services, kind, item_id)
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
    return _ok(
        starred2={
            "artist": await db.list_stars("artist", uid),
            "album": await db.list_stars("album", uid),
            "song": await db.list_stars("song", uid),
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


async def _chart_songs(services: dict[str, Any], count: int) -> list[dict[str, Any]]:
    ytmusic: YTMusicStreamService = services["ytmusic"]
    try:
        tracks = await ytmusic.get_chart_songs(settings.yt_country, limit=count)
    except Exception as exc:  # noqa: BLE001 - a dead chart never breaks Home
        logger.warning("chart songs failed: %s", exc)
        return []
    return [song_from_yt(item) for item in tracks]


@router.get("/getTodaysHits")
async def todays_hits(request: Request, user=Depends(current_user)) -> JSONResponse:
    count = _int(request.query_params.get("count"), 20)
    return _ok(todaysHits={"song": await _chart_songs(_services(request), count)})


@router.get("/getTrendingSongs")
async def trending_songs(request: Request, user=Depends(current_user)) -> JSONResponse:
    count = _int(request.query_params.get("count"), 20)
    return _ok(trendingSongs={"song": await _chart_songs(_services(request), count)})


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
    return _ok(recentlyPlayedSongs={"song": songs})


@router.get("/getSimilarSongs2")
async def similar_songs(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(similarSongs2={"song": []})


@router.get("/getGenres")
async def get_genres(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(genres={"genre": []})


@router.get("/getSongsByGenre")
async def songs_by_genre(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(songsByGenre={"song": []})


@router.get("/getTrendingPlaylists")
async def trending_playlists(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(playlists={"playlist": []})


@router.get("/getYtMusicNewReleases")
async def yt_new_releases(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(albumList2={"album": []})


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
    playlists = []
    for row in rows:
        full = await db.playlist(row["id"], user["user_id"])
        if full:
            playlists.append(_playlist_json(full, full.get("entry") or []))
    return _ok(playlists={"playlist": playlists})


@router.get("/getPlaylist")
async def get_playlist(request: Request, user=Depends(current_user)) -> JSONResponse:
    playlist_id = request.query_params.get("id") or ""
    full = await db.playlist(playlist_id, user["user_id"])
    if not full:
        raise SubsonicError(70, "Playlist not found")
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
    songs = [get_song(song_id) for song_id in song_ids]
    payload = [song for song in songs if song]
    current = request.query_params.get("current")
    position = _float(request.query_params.get("position"), 0.0)
    await db.save_queue(user["user_id"], payload, current, position)
    return _ok()


@router.get("/scrobble")
async def scrobble(request: Request, user=Depends(current_user)) -> JSONResponse:
    services = _services(request)
    song_id = request.query_params.get("id") or ""
    submission = _truthy(request.query_params.get("submission"))
    timestamp = _int(request.query_params.get("time"), int(time.time() * 1000)) // 1000
    song = await services["stream"].song_meta(song_id)
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


@router.get("/getLastfmRecommendations")
async def get_lastfm_recommendations(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(lastfmRecommendations={"shelf": []})


# --- library admin (removed in the slim app) --------------------------------


@router.get("/getScanStatus")
async def scan_status(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(scanStatus={"scanning": False, "count": 0})


@router.get("/startScan")
async def start_scan(request: Request, user=Depends(current_user)) -> JSONResponse:
    return _ok(scanStatus={"scanning": False, "count": 0})
