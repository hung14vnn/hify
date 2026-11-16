"""Per-user Last.fm linking and scrobbling (web auth token flow)."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

import db
from config import settings

logger = logging.getLogger(__name__)

_API = "https://ws.audioscrobbler.com/2.0/"
_AUTH = "https://www.last.fm/api/auth/"


def _sign(params: dict[str, Any], secret: str) -> str:
    payload = "".join(
        f"{key}{params[key]}" for key in sorted(params) if key not in {"format", "callback"}
    )
    return hashlib.md5((payload + secret).encode("utf-8")).hexdigest()


class LastfmService:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._http = client
        self._pending: dict[str, tuple[str, float]] = {}

    @property
    def available(self) -> bool:
        return bool(settings.lastfm_api_key and settings.lastfm_api_secret)

    async def _call(self, method: str, **params: Any) -> dict[str, Any] | None:
        if not self.available:
            return None
        payload = {
            "method": method,
            "api_key": settings.lastfm_api_key,
            **{key: value for key, value in params.items() if value is not None},
        }
        payload["api_sig"] = _sign(payload, settings.lastfm_api_secret)
        payload["format"] = "json"
        try:
            if method.startswith("track."):
                response = await self._http.post(_API, data=payload, timeout=10.0)
            else:
                response = await self._http.get(_API, params=payload, timeout=10.0)
            data = response.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("lastfm %s failed: %s", method, exc)
            return None

    async def connect_url(self, user_id: str) -> str | None:
        data = await self._call("auth.getToken")
        token = (data or {}).get("token")
        if not token:
            return None
        self._pending[str(token)] = (user_id, time.time() + 600)
        return (
            f"{_AUTH}?api_key={settings.lastfm_api_key}"
            f"&cb={quote(settings.lastfm_callback_url, safe='')}&token={token}"
        )

    async def finish(self, token: str) -> str | None:
        pending = self._pending.pop(token, None)
        if pending is None or pending[1] < time.time():
            return None
        user_id = pending[0]
        data = await self._call("auth.getSession", token=token)
        session = (data or {}).get("session") or {}
        key = session.get("key")
        if not key:
            return None
        await db.lastfm_save(user_id, str(key), str(session.get("name") or ""))
        return user_id

    async def status(self, user_id: str) -> dict[str, Any]:
        connection = await db.lastfm_get(user_id)
        return {
            "available": self.available,
            "linked": connection is not None,
            "username": (connection or {}).get("username") or None,
            "scrobbling": bool((connection or {}).get("scrobbling")),
        }

    async def set_scrobbling(self, user_id: str, enabled: bool) -> dict[str, Any]:
        await db.lastfm_toggle(user_id, enabled)
        return await self.status(user_id)

    async def disconnect(self, user_id: str) -> dict[str, Any]:
        await db.lastfm_delete(user_id)
        return await self.status(user_id)

    async def now_playing(self, user_id: str, artist: str, title: str, album: str, duration: int | None) -> None:
        connection = await db.lastfm_get(user_id)
        if not connection or not connection.get("scrobbling"):
            return
        await self._call(
            "track.updateNowPlaying",
            artist=artist,
            track=title,
            album=album or None,
            duration=duration or None,
            sk=connection["session_key"],
        )

    async def scrobble(
        self,
        user_id: str,
        artist: str,
        title: str,
        album: str,
        duration: int | None,
        timestamp: int,
    ) -> None:
        connection = await db.lastfm_get(user_id)
        if not connection or not connection.get("scrobbling"):
            return
        if not artist or not title:
            return
        await self._call(
            "track.scrobble",
            **{
                "artist[0]": artist,
                "track[0]": title,
                "album[0]": album or None,
                "duration[0]": duration or None,
                "timestamp[0]": timestamp,
                "sk": connection["session_key"],
            },
        )
