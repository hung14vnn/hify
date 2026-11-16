from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _csv(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _env(name).split(",") if part.strip())


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    music_db_path: Path = Path(_env("MUSIC_DB_PATH", str(BASE_DIR / "data" / "music.db")))
    netflix_db_path: Path = Path(
        _env("NETFLIX_DB_PATH", str(BASE_DIR.parent / "GroupCalendarCompanion" / "data" / "db.json"))
    )
    session_ttl_days: int = _env_int("SESSION_TTL_DAYS", 30)

    # Eclipse Music add-on base URLs (https://api.eclipsemusic.app/addon/<key>/music[/manifest.json]).
    # Several add-ons are tried in order, like Stream-Bridge.
    eclipse_addons: tuple[str, ...] = _csv("ECLIPSE_ADDON_URLS")
    eclipse_bearer_token: str = _env("ECLIPSE_BEARER_TOKEN", "")
    eclipse_quality: str = _env("ECLIPSE_QUALITY", "HI_RES_LOSSLESS")
    eclipse_duration_tolerance_s: float = _env_float("ECLIPSE_DURATION_TOLERANCE_S", 8.0)
    eclipse_timeout_s: float = _env_float("ECLIPSE_TIMEOUT_S", 8.0)
    # Local add-ons (or a LAN server) are loopback/private; only enable in dev.
    allow_private_stream_hosts: bool = _env_bool("ALLOW_PRIVATE_STREAM_HOSTS", False)

    lastfm_api_key: str = _env("LASTFM_API_KEY")
    lastfm_api_secret: str = _env("LASTFM_API_SECRET")
    lastfm_callback_url: str = _env(
        "LASTFM_CALLBACK_URL", "http://127.0.0.1:8089/api/v1/lastfm/callback"
    )
    lastfm_return_url: str = _env("LASTFM_RETURN_URL", "http://127.0.0.1:5180/profile")

    cors_origins: tuple[str, ...] = _csv("CORS_ORIGINS") or ("*",)
    yt_country: str = _env("YT_COUNTRY", "VN")
    yt_format: str = _env("YT_FORMAT", "opus")
    # HTTP CONNECT proxy for YouTube traffic, usually a container sharing the
    # ProtonVPN namespace (YouTube blocks the VM's direct datacenter IP).
    outbound_proxy: str = _env("OUTBOUND_PROXY")
    http_timeout_s: float = _env_float("HTTP_TIMEOUT_S", 20.0)


settings = Settings()
