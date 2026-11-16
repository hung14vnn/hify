"""Remote playback: FLAC bridge first, YouTube Music as the fallback."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import unicodedata
from typing import AsyncIterator
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi.responses import StreamingResponse

from config import settings
from errors import SubsonicError
from models import song_from_spotify, song_from_yt
import db
from providers.flac_bridge import FlacBridge
from providers.meta import get_song, remember
from providers.spotapi_client import SpotApiClient
from providers.ytmusic_stream_service import YTMusicStreamService

logger = logging.getLogger(__name__)

_STREAM_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
_MAX_REDIRECTS = 3


def _blocked_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


_VIDEO_NOISE = re.compile(
    r"\b(official|video|audio|lyrics?|mv|m/v|visuali[sz]er|hd|4k|topic)\b", re.I
)


def _plain(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _clean_artist(value: str) -> str:
    text = re.sub(r"\s*-\s*topic\s*$", "", value, flags=re.I)
    text = re.sub(r"\s*(vevo|official)\s*$", "", text, flags=re.I)
    return text.strip() or value


def _clean_youtube_title(title: str, artist: str) -> str:
    """Drop video-only noise so the Eclipse search sees the song's real title."""
    text = re.sub(
        r"[(\[][^)\]]*[)\]]",
        lambda match: "" if _VIDEO_NOISE.search(match.group(0)) else match.group(0),
        title,
    )
    parts = [part.strip() for part in re.split(r"\s*[|•]\s*", text) if part.strip()]
    if len(parts) > 1:
        keep = [part for part in parts if not _VIDEO_NOISE.search(part)]
        if keep:
            artists = {_plain(artist), _plain(_clean_artist(artist))}
            without_artist = [part for part in keep if _plain(part) not in artists]
            text = (without_artist or keep)[-1]
        else:
            text = parts[-1]
    return re.sub(r"\s+", " ", text).strip(" -–—") or title


async def _host_allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return False
    if settings.allow_private_stream_hosts:
        return True
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        )
    except OSError:
        return False
    return bool(infos) and not any(_blocked_ip(info[4][0]) for info in infos)


class StreamService:
    def __init__(
        self,
        http: httpx.AsyncClient,
        spotify: SpotApiClient,
        ytmusic: YTMusicStreamService,
        flac: FlacBridge,
    ) -> None:
        self._http = http
        self._spotify = spotify
        self._ytmusic = ytmusic
        self._flac = flac

    # --- metadata ----------------------------------------------------------

    async def song_meta(self, song_id: str) -> dict | None:
        cached = get_song(song_id)
        if cached is not None:
            return cached
        if song_id.startswith("st-"):
            track_id = song_id[3:]
            if not track_id:
                return None
            track = await self._spotify.get_track(track_id)
            song = song_from_spotify(track)
            remember(song)
            return song
        if song_id.startswith("ytv-"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", song_id[4:]):
                return None
            raw = await self._ytmusic.video_meta(song_id[4:])
            return song_from_yt(raw, video=True)
        if song_id.startswith("yt-"):
            video_id = song_id[3:]
            if not video_id:
                return None
            raw = await self._ytmusic.track_meta(video_id)
            if raw is None:
                return None
            return song_from_yt(raw)
        return None

    @staticmethod
    def _artist(song: dict) -> str:
        artists = song.get("artists")
        if isinstance(artists, list) and artists:
            first = artists[0]
            if isinstance(first, dict) and first.get("name"):
                return str(first["name"])
        return str(song.get("artist") or "")

    # --- HTTP proxying -----------------------------------------------------

    async def _open(self, url: str, range_header: str | None) -> tuple[httpx.Response, str]:
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            if not await _host_allowed(current):
                raise ValueError("blocked host")
            request = self._http.build_request(
                "GET", current, headers={"Range": range_header} if range_header else None
            )
            response = await self._http.send(request, stream=True)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise ValueError("redirect without location")
                current = urljoin(current, location)
                continue
            return response, current
        raise ValueError("too many redirects")

    async def _proxy_direct(
        self,
        url: str,
        range_header: str | None,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> StreamingResponse:
        response, _ = await self._open(url, range_header)
        if response.status_code not in {200, 206}:
            await response.aclose()
            raise SubsonicError(70, "Source returned no audio")
        headers = {
            key: response.headers[key] for key in _STREAM_HEADERS if key in response.headers
        }
        headers.setdefault("Content-Type", content_type)
        headers["Content-Encoding"] = "identity"
        if extra_headers:
            headers.update(extra_headers)

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(
            body(),
            status_code=response.status_code,
            headers=headers,
            media_type=headers.get("Content-Type", content_type),
        )

    async def _proxy_ffmpeg(
        self,
        url: str,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> StreamingResponse:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel", "error",
            "-i", url,
            "-c:a", "copy",
            "-f", "flac",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def body() -> AsyncIterator[bytes]:
            try:
                while True:
                    chunk = await proc.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            except (asyncio.CancelledError, GeneratorExit):
                pass
            finally:
                if proc.returncode is None:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

        headers = {
            "Content-Type": content_type,
            "Content-Encoding": "identity",
            "Accept-Ranges": "none",
        }
        if extra_headers:
            headers.update(extra_headers)

        return StreamingResponse(
            body(),
            status_code=200,
            headers=headers,
            media_type=content_type,
        )

    async def _proxy_yt(self, video_id: str, range_header: str | None) -> StreamingResponse:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                chunks, headers, status = await self._ytmusic.proxy_stream(
                    video_id, range_header=range_header, fmt=settings.yt_format
                )
                break
            except httpx.HTTPStatusError as exc:
                last_error = exc
                logger.warning("yt stream attempt %s failed: %s", attempt + 1, exc)
                await asyncio.sleep(0.4 * (attempt + 1))
        else:
            raise SubsonicError(70, "YouTube Music is unavailable right now") from last_error
        out = {key: headers[key] for key in _STREAM_HEADERS if key in headers}
        out["Content-Encoding"] = "identity"
        return StreamingResponse(
            chunks,
            status_code=status,
            headers=out,
            media_type=headers.get("Content-Type", "application/octet-stream"),
        )

    # --- public API --------------------------------------------------------

    async def stream(
        self, song_id: str, range_header: str | None, user_id: str | None = None
    ) -> StreamingResponse:
        if song_id.startswith("kr-"):
            raise SubsonicError(70, "Karaoke is not available")
        if song_id.startswith("ytv-"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", song_id[4:]):
                raise SubsonicError(70, "Invalid YouTube video id")
            return await self._proxy_yt(song_id, range_header)
        is_yt = song_id.startswith("yt-")
        if not is_yt and not song_id.startswith("st-"):
            raise SubsonicError(70, "Invalid id type")
        quality = (
            await db.get_quality(user_id) if user_id else settings.eclipse_quality
        )
        meta = await self.song_meta(song_id)
        if meta is None:
            if is_yt:
                # No metadata means the FLAC bridge cannot be searched; play YouTube.
                return await self._proxy_yt(song_id[3:], range_header)
            raise SubsonicError(70, "Song not found")
        artist = self._artist(meta)
        title = str(meta.get("title") or "")
        duration = meta.get("duration")
        if is_yt:
            # YouTube titles/authors carry video noise, and music videos run longer
            # than the song, so match the provider on cleaned title + artist only.
            flac_artist = _clean_artist(artist)
            flac_title = _clean_youtube_title(title, artist)
            flac_duration = None
        else:
            flac_artist, flac_title, flac_duration = artist, title, duration
        flac = await self._flac.resolve(
            artist=flac_artist,
            title=flac_title,
            duration_s=flac_duration,
            quality=quality,
        )
        if flac:
            try:
                quality_headers: dict[str, str] = {}
                if flac.bit_depth:
                    quality_headers["X-Audio-Bit-Depth"] = str(flac.bit_depth)
                if flac.sample_rate:
                    quality_headers["X-Audio-Sample-Rate"] = str(flac.sample_rate)
                if flac.is_dash:
                    return await self._proxy_ffmpeg(
                        flac.url, flac.content_type, quality_headers
                    )
                return await self._proxy_direct(
                    flac.url, range_header, flac.content_type, quality_headers
                )
            except (httpx.HTTPError, ValueError, SubsonicError, OSError) as exc:
                logger.warning("flac_bridge: proxy failed, falling back to YT: %s", exc)
        if is_yt:
            return await self._proxy_yt(song_id[3:], range_header)
        info = await self._ytmusic.search(
            artist, title, fmt=settings.yt_format, expected_duration_s=duration
        )
        return await self._proxy_yt(info.video_id, range_header)
