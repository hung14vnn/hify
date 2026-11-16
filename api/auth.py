"""Accounts come from the netflix-clone project (read-only).

netflix-clone stores users in ``data/db.json`` with scrypt password hashes
(``scrypt$N$r$p$salt$hash``, base64url) and exposes no token endpoint.
This service reads that file read-only, verifies credentials locally and
issues its own bearer sessions. The netflix user id (``usr_...``) is the
stable key used by every table here, so migrating to a real token later
only replaces this module.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Request

import db
from config import settings
from errors import SubsonicError

_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": 0.0, "users": {}, "by_id": {}}


def _b64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def verify_scrypt(password: str, encoded: str) -> bool:
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    _, n, r, p, salt_part, hash_part = parts
    try:
        salt = _b64url(salt_part)
        expected = _b64url(hash_part)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


def _load_users() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    path: Path = settings.netflix_db_path
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}, {}
    with _lock:
        if _cache["mtime"] == mtime:
            return _cache["users"], _cache["by_id"]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}, {}
        users = raw.get("users")
        if not isinstance(users, list):
            users = []
        by_email = {
            str(user.get("email", "")).lower(): user
            for user in users
            if isinstance(user, dict) and user.get("email")
        }
        by_id = {
            str(user.get("id")): user
            for user in users
            if isinstance(user, dict) and user.get("id")
        }
        _cache.update(mtime=mtime, users=by_email, by_id=by_id)
        return by_email, by_id


def find_user(identifier: str) -> dict[str, Any] | None:
    by_email, _ = _load_users()
    key = identifier.strip().lower()
    if not key:
        return None
    return by_email.get(key)


def user_by_id(user_id: str) -> dict[str, Any] | None:
    _, by_id = _load_users()
    return by_id.get(user_id)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(user.get("id")),
        "name": str(user.get("name") or ""),
        "email": str(user.get("email") or ""),
        "role": "admin" if user.get("role") == "admin" else "user",
    }


async def login(identifier: str, password: str) -> tuple[str, dict[str, Any]]:
    user = find_user(identifier)
    if user is None or not verify_scrypt(password, str(user.get("passwordHash") or "")):
        raise SubsonicError(40, "Wrong username or password.")
    status = str(user.get("status") or "active")
    if status == "pending":
        raise SubsonicError(40, "This account is waiting for admin approval.")
    if status != "active":
        raise SubsonicError(40, "This account is locked.")
    public = _public(user)
    token = secrets.token_urlsafe(32)
    await db.create_session(hash_token(token), public)
    return token, public


def _token_from_request(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.query_params.get("apiKey") or "").strip()


async def current_user(request: Request) -> dict[str, Any]:
    token = _token_from_request(request)
    if not token:
        raise SubsonicError(40, "Wrong username or password.")
    session = await db.get_session(hash_token(token))
    if session is None:
        raise SubsonicError(40, "Session expired. Sign in again.")
    user = user_by_id(str(session["user_id"]))
    if user is None or str(user.get("status")) != "active":
        await db.delete_session(hash_token(token))
        raise SubsonicError(40, "This account is locked.")
    return {
        "user_id": str(session["user_id"]),
        "username": str(session["username"]),
        "email": str(session["email"]),
        "role": str(session["role"]),
    }


async def current_user_http(request: Request) -> dict[str, Any]:
    """HTTP-facing variant: raises 401 instead of a Subsonic envelope."""
    from fastapi import HTTPException

    try:
        return await current_user(request)
    except SubsonicError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
