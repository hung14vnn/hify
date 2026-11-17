"""Remote playback: FLAC bridge first, YouTube Music as the fallback."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import unicodedata
from typing import AsyncIterator
from urllib.parse import quote, urljoin, urlsplit

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


def _content_disposition(artist: str, title: str, content_type: str) -> str:
    ext = "flac" if "flac" in content_type else ("opus" if ("webm" in content_type or "ogg" in content_type) else ("m4a" if "mp4" in content_type else "mp3"))
    name = f"{artist} - {title}".strip(" -") or "track"
    # The plain `filename` must be ASCII: header values are latin-1, and \w alone keeps
    # letters such as "ơ"/"ư", which made every Vietnamese title a 500. Strip the
    # accents for it; the full name still goes in `filename*`.
    folded = unicodedata.normalize("NFKD", name.replace("đ", "d").replace("Đ", "D"))
    folded = folded.encode("ascii", "ignore").decode("ascii")
    safe_ascii = re.sub(r"[^\w\s.-]", "", folded, flags=re.ASCII).strip() or "track"
    filename_ascii = f"{safe_ascii}.{ext}"
    filename_utf8 = quote(f"{name}.{ext}")
    return f'attachment; filename="{filename_ascii}"; filename*=UTF-8\'\'{filename_utf8}'


def _ffmpeg_output(content_type: str, seeking: bool) -> tuple[str, str, str]:
    """(codec, muxer, Content-Type) for an ffmpeg remux, keeping the source codec.

    Every output here restarts its timestamps at 0 after `-ss` (verified with
    ffprobe), which the player relies on to add its own offset.
    - FLAC: a stream copy keeps the source's frame numbers and STREAMINFO, so a seeked
      stream claims to start at the offset with the full length, and browsers disagree
      on the clock. Re-encoding is still bit-exact and ~100x realtime at 24/192.
    - AAC (the lossless source's HIGH, YouTube m4a): copied into ADTS. It used to be
      re-encoded to FLAC, several times the bytes for no quality gain.
    - Opus/Vorbis (YouTube webm): copied into Ogg, which also gets the right length.
    """
    ct = (content_type or "").lower()
    if "flac" in ct:
        return ("flac" if seeking else "copy"), "flac", "audio/flac"
    # YouTube's StreamInfo carries the file extension ("m4a", "webm"), not a MIME type.
    if "mp4" in ct or "aac" in ct or "m4a" in ct:
        return "copy", "adts", "audio/aac"
    if "mpeg" in ct or "mp3" in ct:
        return "copy", "mp3", "audio/mpeg"
    if "webm" in ct or "ogg" in ct or "opus" in ct:
        return "copy", "ogg", "audio/ogg"
    # Unknown codec: a lossless FLAC encode always plays.
    return "flac", "flac", "audio/flac"


def is_ios_user_agent(ua: str | None) -> bool:
    if not ua:
        return False
    low = ua.lower()
    return any(x in low for x in ("iphone", "ipad", "ipod", "cfnetwork", "applecoremedia"))


def _effective_fmt(format_param: str | None, user_agent: str | None) -> str:
    if format_param:
        low = format_param.lower().strip()
        if low in {"m4a", "aac", "mp4"}:
            return "m4a"
        if low in {"opus", "webm", "ogg"}:
            return "opus"
        return low
    return "m4a" if is_ios_user_agent(user_agent) else settings.yt_format


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
        range_match = re.match(r"^bytes=(\d+)-(\d+)?$", range_header.strip()) if range_header else None
        response, _ = await self._open(url, range_header)
        if response.status_code not in {200, 206}:
            await response.aclose()
            raise SubsonicError(70, "Source returned no audio")
        headers = {
            key: response.headers[key] for key in _STREAM_HEADERS if key in response.headers
        }
        headers.setdefault("Content-Type", content_type)
        headers["Content-Encoding"] = "identity"
        headers["Accept-Ranges"] = "bytes"
        if extra_headers:
            headers.update(extra_headers)

        # If client requested a Range but upstream returned full 200 OK, slice on-the-fly to return 206
        if range_match and response.status_code == 200:
            total_str = response.headers.get("Content-Length")
            total = int(total_str) if total_str and total_str.isdigit() else None
            req_start = int(range_match.group(1))
            req_end = int(range_match.group(2)) if range_match.group(2) else (total - 1 if total else None)

            if req_start > 0 or req_end is not None:
                status_code = 206
                headers["Content-Range"] = f"bytes {req_start}-{req_end if req_end is not None else ''}/{total if total else '*'}"
                if req_end is not None:
                    headers["Content-Length"] = str(req_end - req_start + 1)

                async def slice_body() -> AsyncIterator[bytes]:
                    skipped = 0
                    yielded = 0
                    to_yield = (req_end - req_start + 1) if req_end is not None else None
                    try:
                        async for chunk in response.aiter_bytes():
                            if skipped < req_start:
                                needed = req_start - skipped
                                if len(chunk) <= needed:
                                    skipped += len(chunk)
                                    continue
                                chunk = chunk[needed:]
                                skipped = req_start
                            if to_yield is not None:
                                remaining = to_yield - yielded
                                if remaining <= 0:
                                    break
                                if len(chunk) > remaining:
                                    chunk = chunk[:remaining]
                            yielded += len(chunk)
                            yield chunk
                    finally:
                        await response.aclose()

                return StreamingResponse(
                    slice_body(),
                    status_code=status_code,
                    headers=headers,
                    media_type=headers.get("Content-Type", content_type),
                )

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
        time_offset: float | None = None,
        user_agent: str | None = None,
    ) -> StreamingResponse:
        seeking = bool(time_offset and time_offset > 0)
        cmd = ["ffmpeg", "-loglevel", "error"]
        if user_agent:
            cmd.extend(["-user_agent", user_agent])
        if seeking:
            cmd.extend(["-ss", f"{time_offset:.2f}"])
        codec, muxer, out_type = _ffmpeg_output(content_type, seeking)
        cmd.extend([
            "-i", url,
            "-c:a", codec,
            "-f", muxer,
            "pipe:1",
        ])
        async def body() -> AsyncIterator[bytes]:
            # Spawned here, not before the response: a client that aborts first (fast
            # scrubbing cancels every previous seek) never starts iterating, and a
            # process spawned outside would never reach this cleanup. stderr is
            # discarded: an undrained pipe fills up and blocks ffmpeg mid-stream.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
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
            "Content-Type": out_type,
            "Content-Encoding": "identity",
            "Accept-Ranges": "none",
        }
        if extra_headers:
            headers.update(extra_headers)

        return StreamingResponse(
            body(),
            status_code=200,
            headers=headers,
            media_type=out_type,
        )

    async def _proxy_yt(
        self,
        video_id: str,
        range_header: str | None,
        time_offset: float | None = None,
        fmt: str | None = None,
    ) -> StreamingResponse:
        format_name = fmt or settings.yt_format
        if time_offset and time_offset > 0:
            # The player asked for a server-side seek (it took this for a FLAC stream).
            # Ignoring the offset would play from 0:00 while its clock shows the offset,
            # so cut the YouTube audio here too, as the same restart-at-0 FLAC stream.
            info = await self._ytmusic.stream_info(video_id, fmt=format_name)
            return await self._proxy_ffmpeg(
                info.audio_url,
                info.content_type,
                time_offset=time_offset,
                user_agent=info.http_headers.get("User-Agent"),
            )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                chunks, headers, status = await self._ytmusic.proxy_stream(
                    video_id, range_header=range_header, fmt=format_name
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
        self,
        song_id: str,
        range_header: str | None,
        user_id: str | None = None,
        time_offset: float | None = None,
        user_agent: str | None = None,
        format: str | None = None,
    ) -> StreamingResponse:
        effective_fmt = _effective_fmt(format, user_agent)
        yt_kwargs: dict[str, Any] = {}
        if time_offset is not None:
            yt_kwargs["time_offset"] = time_offset
        if effective_fmt != settings.yt_format:
            yt_kwargs["fmt"] = effective_fmt

        if song_id.startswith("kr-"):
            raise SubsonicError(70, "Karaoke is not available")
        if song_id.startswith("ytv-"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", song_id[4:]):
                raise SubsonicError(70, "Invalid YouTube video id")
            return await self._proxy_yt(song_id, range_header, **yt_kwargs)
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
                return await self._proxy_yt(song_id[3:], range_header, **yt_kwargs)
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
        # LOW: data saver, straight to YouTube (yt-dlp) without asking the lossless source.
        flac = (
            None
            if quality == db.YT_ONLY_QUALITY
            else await self._flac.resolve(
                artist=flac_artist,
                title=flac_title,
                duration_s=flac_duration,
                quality=quality,
            )
        )
        if flac:
            try:
                quality_headers: dict[str, str] = {}
                if flac.bit_depth:
                    quality_headers["X-Audio-Bit-Depth"] = str(flac.bit_depth)
                if flac.sample_rate:
                    quality_headers["X-Audio-Sample-Rate"] = str(flac.sample_rate)
                if flac.is_dash or (time_offset and time_offset > 0):
                    return await self._proxy_ffmpeg(
                        flac.url, flac.content_type, quality_headers, time_offset=time_offset
                    )
                return await self._proxy_direct(
                    flac.url, range_header, flac.content_type, quality_headers
                )
            except (httpx.HTTPError, ValueError, SubsonicError, OSError) as exc:
                logger.warning("flac_bridge: proxy failed, falling back to YT: %s", exc)
        if is_yt:
            return await self._proxy_yt(song_id[3:], range_header, **yt_kwargs)
        info = await self._ytmusic.search(
            artist, title, fmt=effective_fmt, expected_duration_s=duration
        )
        return await self._proxy_yt(info.video_id, range_header, **yt_kwargs)

    async def download(
        self,
        song_id: str,
        range_header: str | None = None,
        lossless: bool = False,
        user_id: str | None = None,
        user_agent: str | None = None,
        format: str | None = None,
    ) -> StreamingResponse:
        """Download stream: if lossless=True, resolves through FLAC bridge first (TIDAL Hi-Res),
        falling back to yt-dlp. If lossless=False, downloads directly through yt-dlp."""
        effective_fmt = _effective_fmt(format, user_agent)
        yt_kwargs: dict[str, Any] = {}
        if effective_fmt != settings.yt_format:
            yt_kwargs["fmt"] = effective_fmt

        if song_id.startswith("kr-"):
            raise SubsonicError(70, "Karaoke is not available")
        if song_id.startswith("ytv-"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", song_id[4:]):
                raise SubsonicError(70, "Invalid YouTube video id")
            resp = await self._proxy_yt(song_id, range_header, **yt_kwargs)
            default_content_type = "audio/mp4" if effective_fmt == "m4a" else "audio/webm"
            content_type = resp.headers.get("Content-Type", default_content_type)
            resp.headers["Content-Disposition"] = _content_disposition("YouTube", song_id[4:], content_type)
            return resp

        is_yt = song_id.startswith("yt-")
        if not is_yt and not song_id.startswith("st-"):
            raise SubsonicError(70, "Invalid id type")

        meta = await self.song_meta(song_id)
        if meta is None:
            if is_yt:
                video_id = song_id[3:]
                resp = await self._proxy_yt(video_id, range_header, **yt_kwargs)
                default_content_type = "audio/mp4" if effective_fmt == "m4a" else "audio/webm"
                content_type = resp.headers.get("Content-Type", default_content_type)
                resp.headers["Content-Disposition"] = _content_disposition("YouTube", video_id, content_type)
                return resp
            raise SubsonicError(70, "Song not found")

        artist = self._artist(meta)
        title = str(meta.get("title") or "")
        duration = meta.get("duration")

        if lossless:
            quality = (
                await db.get_quality(user_id) if user_id else settings.eclipse_quality
            )
            if is_yt:
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
                    disposition = _content_disposition(artist, title, flac.content_type)
                    quality_headers = {"Content-Disposition": disposition}
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
                    logger.warning("flac_bridge: download proxy failed, falling back to YT: %s", exc)

        # Fallback or standard download: yt-dlp (YouTube Music)
        if is_yt:
            video_id = song_id[3:]
        else:
            info = await self._ytmusic.search(
                artist, title, fmt=effective_fmt, expected_duration_s=duration
            )
            video_id = info.video_id

        resp = await self._proxy_yt(video_id, range_header, **yt_kwargs)
        default_content_type = "audio/mp4" if effective_fmt == "m4a" else "audio/webm"
        content_type = resp.headers.get("Content-Type", default_content_type)
        resp.headers["Content-Disposition"] = _content_disposition(artist, title, content_type)
        return resp
