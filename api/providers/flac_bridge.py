"""FLAC playback through Eclipse Music add-ons (the Stream-Bridge approach).

Each add-on is a personal HTTPS manifest URL; its root exposes
``manifest.json``, ``resolve-isrc``, ``resolve``, ``search`` and
``stream/<id>``. Only direct audio URLs are accepted here: HLS/DASH
responses are rejected because the player is a plain <audio> element.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

_MANIFEST_TTL = 10 * 60.0
_RESOLVE_TTL = 30 * 60.0
_URL_TTL = 10 * 60.0


@dataclass(slots=True)
class FlacStream:
    url: str
    content_type: str
    duration_s: float | None
    provider: str
    bit_depth: int | None = None
    sample_rate: int | None = None
    is_dash: bool = False


def _norm(value: Any) -> str:
    text = str(value or "").lower().replace("&", " and ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _content_type(codec: str, fmt: str, container: str, url: str) -> str | None:
    values = {codec, fmt, container}
    if values & {"flac", "audio/flac", "audio/x-flac"} or re.search(
        r"\.flac(?:[?#]|$)", url, re.I
    ):
        return "audio/flac"
    if values & {"m4a", "mp4", "alac", "audio/mp4"}:
        return "audio/mp4"
    if values & {"aac", "audio/aac"}:
        return "audio/aac"
    if values & {"mp3", "mpeg", "audio/mpeg"} or re.search(
        r"\.mp3(?:[?#]|$)", url, re.I
    ):
        return "audio/mpeg"
    if values & {"opus", "ogg", "vorbis", "audio/ogg"} or re.search(
        r"\.(?:opus|ogg)(?:[?#]|$)", url, re.I
    ):
        return "audio/ogg"
    return None


def _base(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if value.lower().endswith("/manifest.json"):
        value = value[: -len("/manifest.json")]
    return value


class FlacBridge:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._http = client
        self._addons = tuple(_base(item) for item in settings.eclipse_addons)
        self._manifests: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._resolved: dict[str, tuple[float, str, str]] = {}
        self._urls: dict[str, tuple[float, FlacStream]] = {}
        self._access_tokens: dict[str, tuple[float, str]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._addons)

    async def _get_access_token(self, addon_id: str) -> str | None:
        if not settings.eclipse_bearer_token:
            return None
        now = time.monotonic()
        cached = self._access_tokens.get(addon_id)
        if cached and cached[0] > now:
            return cached[1]
        try:
            resp = await self._http.post(
                "https://api.eclipsemusic.app/api/addon-access/token",
                params={"addon": addon_id},
                headers={"Authorization": f"Bearer {settings.eclipse_bearer_token}"},
                timeout=settings.eclipse_timeout_s,
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token")
                expires_in = float(data.get("expiresIn") or 300)
                ttl = max(60.0, expires_in - 30.0)
                self._access_tokens[addon_id] = (now + ttl, token)
                return token
            logger.warning("Failed to fetch Eclipse access token: %s %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("Failed to fetch Eclipse access token: %s", exc)
        return None

    async def _json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        addon_id: str | None = None,
    ) -> dict[str, Any] | None:
        headers = {}
        if addon_id:
            token = await self._get_access_token(addon_id)
            if token:
                headers["X-Eclipse-Access"] = token
        try:
            response = await self._http.get(
                url, params=params, headers=headers if headers else None, timeout=settings.eclipse_timeout_s
            )
            if response.status_code >= 400:
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    async def _manifest(self, base: str) -> dict[str, Any] | None:
        now = time.monotonic()
        cached = self._manifests.get(base)
        if cached and cached[0] > now:
            return cached[1]
        data = await self._json(base + "/manifest.json")
        if data is not None and isinstance(data.get("resources"), list):
            resources = {str(item) for item in data["resources"]}
            data = {**data, "resources": resources}
        else:
            data = None
        self._manifests[base] = (now + _MANIFEST_TTL, data)
        return data

    @staticmethod
    def _match(
        title: str, artist: str, duration_s: float | None, item: dict[str, Any]
    ) -> bool:
        if _norm(item.get("title") or item.get("name")) != _norm(title):
            return False
        candidate_artists = item.get("artists")
        if isinstance(candidate_artists, list) and candidate_artists:
            names = [
                _norm(entry.get("name") if isinstance(entry, dict) else entry)
                for entry in candidate_artists
            ]
        else:
            names = [_norm(item.get("artist"))]
        if _norm(artist) not in names and not any(_norm(artist) in name for name in names if name):
            return False
        candidate_duration = item.get("durationMs")
        if candidate_duration is None and item.get("duration") is not None:
            try:
                candidate_duration = float(item["duration"]) * 1000
            except (TypeError, ValueError):
                candidate_duration = None
        if duration_s and candidate_duration:
            try:
                if abs(float(candidate_duration) / 1000 - duration_s) > settings.eclipse_duration_tolerance_s:
                    return False
            except (TypeError, ValueError):
                pass
        return True

    async def _track_id(
        self, base: str, manifest: dict[str, Any], artist: str, title: str, duration_s: float | None
    ) -> str | None:
        addon_id = manifest.get("id")
        resources: set[str] = manifest.get("resources") or set()
        if "resolve" in resources:
            params = {"title": title, "artist": artist}
            if duration_s:
                params["durationMs"] = int(duration_s * 1000)
            data = await self._json(base + "/resolve", params, addon_id=addon_id)
            if data:
                item = data.get("item") or data.get("track") or data.get("result")
                if isinstance(item, dict) and item.get("id") and self._match(title, artist, duration_s, item):
                    return str(item["id"])
                if isinstance(data.get("result"), dict) and data["result"].get("item"):
                    item = data["result"]["item"]
                    if isinstance(item, dict) and item.get("id") and self._match(title, artist, duration_s, item):
                        return str(item["id"])
        if "search" in resources:
            data = await self._json(base + "/search", {"q": f"{artist} {title}"}, addon_id=addon_id)
            tracks = data.get("tracks") if data else None
            if isinstance(tracks, list):
                for item in tracks:
                    if isinstance(item, dict) and item.get("id") and self._match(title, artist, duration_s, item):
                        return str(item["id"])
        return None

    @staticmethod
    def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
        current: dict[str, Any] = payload
        for _ in range(3):
            if any(key in current for key in ("url", "streamURL", "streamUrl")):
                return current
            for key in ("data", "stream", "result"):
                nested = current.get(key)
                if isinstance(nested, dict):
                    current = nested
                    break
            else:
                return current
        return current

    async def _stream_url(
        self, base: str, track_id: str, quality: str, addon_id: str | None = None
    ) -> FlacStream | None:
        key = f"{base}|{track_id}|{quality}"
        now = time.monotonic()
        cached = self._urls.get(key)
        if cached and cached[0] > now:
            return cached[1]
        data = await self._json(f"{base}/stream/{track_id}", {"quality": quality}, addon_id=addon_id)
        if not data:
            return None
        stream = self._unwrap(data)
        url = str(stream.get("url") or stream.get("streamURL") or stream.get("streamUrl") or "")
        if not re.match(r"^https?://", url, re.I):
            return None

        is_dash = bool(
            re.search(r"\.(m3u8|mpd)(?:[?#]|$)", url, re.I)
            or str(stream.get("manifest") or "").lower() in {"dash", "hls"}
        )
        if not is_dash:
            if stream.get("manifest") and str(stream.get("manifest")).lower() != "none":
                return None

        codec = str(stream.get("codec") or "").lower()
        fmt = str(stream.get("format") or "").lower()
        container = str(stream.get("container") or "").lower()
        content_type = _content_type(codec, fmt, container, url)
        if content_type is None:
            if is_dash and ("flac" in fmt or "flac" in codec or "flac" in container):
                content_type = "audio/flac"
            else:
                return None
        lossless = content_type == "audio/flac"
        if quality.upper() in {"HI_RES_LOSSLESS", "LOSSLESS"} and not lossless:
            return None
        duration = stream.get("duration")
        expires = time.monotonic() + _URL_TTL
        expiry = stream.get("expiresAt")
        if expiry:
            try:
                expiry_s = float(expiry)
                if expiry_s < 1e12:
                    expiry_s *= 1000
                remaining = (expiry_s - time.time() * 1000) / 1000
                expires = min(expires, time.monotonic() + max(remaining - 5, 0))
            except (TypeError, ValueError):
                pass
        result = FlacStream(
            url=url,
            content_type=content_type,
            duration_s=float(duration) if duration else None,
            provider=base,
            bit_depth=int(stream["bitDepth"]) if stream.get("bitDepth") else None,
            sample_rate=int(stream["sampleRate"]) if stream.get("sampleRate") else None,
            is_dash=is_dash,
        )
        self._urls[key] = (expires, result)
        logger.info(
            "flac_bridge: %s (dash=%s) for %s via %s (%s)",
            content_type,
            is_dash,
            track_id,
            base,
            quality,
        )
        return result

    async def resolve(
        self,
        *,
        artist: str,
        title: str,
        duration_s: float | None = None,
        isrc: str | None = None,
        quality: str | None = None,
    ) -> FlacStream | None:
        if not self.enabled or not artist or not title:
            return None
        effective = (quality or settings.eclipse_quality).upper()
        key = f"{_norm(artist)}|{_norm(title)}|{round(duration_s or 0)}|{effective}"
        now = time.monotonic()
        cached = self._resolved.get(key)
        for base in self._addons:
            manifest = await self._manifest(base)
            if manifest is None:
                continue
            addon_id = manifest.get("id")
            if cached and cached[0] > now:
                cached_base, track_id = cached[1], cached[2]
                if cached_base == base:
                    stream = await self._stream_url(base, track_id, effective, addon_id=addon_id)
                    if stream:
                        return stream
            track_id = None
            resources: set[str] = manifest.get("resources") or set()
            if isrc and "isrc" in resources:
                data = await self._json(base + "/resolve-isrc", {"isrc": isrc}, addon_id=addon_id)
                if data and data.get("trackId"):
                    track_id = str(data["trackId"])
            if not track_id:
                track_id = await self._track_id(base, manifest, artist, title, duration_s)
            if not track_id:
                continue
            stream = await self._stream_url(base, track_id, effective, addon_id=addon_id)
            if stream:
                self._resolved[key] = (now + _RESOLVE_TTL, base, track_id)
                return stream
        return None
