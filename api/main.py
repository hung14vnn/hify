from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
import db
from config import BASE_DIR, settings
from errors import SubsonicError
from lastfm import LastfmService
from providers.flac_bridge import FlacBridge
from providers.spotapi_client import SpotApiClient
from providers.streaming import StreamService
from providers.ytmusic_stream_service import YTMusicStreamService
from subsonic import router as subsonic_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("music")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(db.init_db)
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout_s, read=None),
        follow_redirects=False,
        trust_env=False,
        proxy=settings.outbound_proxy or None,
        headers={"User-Agent": "hify/1.0"},
    )
    spotify = SpotApiClient()
    ytmusic = YTMusicStreamService(http)
    flac = FlacBridge(http)
    app.state.services = {
        "http": http,
        "spotify": spotify,
        "ytmusic": ytmusic,
        "flac": flac,
        "stream": StreamService(http, spotify, ytmusic, flac),
        "lastfm": LastfmService(http),
    }
    logger.info(
        "ready: db=%s netflix_db=%s flac_addons=%d lastfm=%s proxy=%s",
        settings.music_db_path,
        settings.netflix_db_path,
        len(settings.eclipse_addons),
        "on" if settings.lastfm_api_key else "off",
        "on" if settings.outbound_proxy else "off",
    )
    try:
        yield
    finally:
        await http.aclose()


app = FastAPI(title="hify", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(SubsonicError)
async def subsonic_error(request: Request, exc: SubsonicError) -> JSONResponse:
    payload = {
        "subsonic-response": {
            "status": "failed",
            "version": "1.16.1",
            "error": {"code": exc.code, "message": exc.message},
        }
    }
    return JSONResponse(payload)


app.include_router(subsonic_router, prefix="/rest")


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "flac": bool(settings.eclipse_addons),
        "lastfm": bool(settings.lastfm_api_key and settings.lastfm_api_secret),
    }


@app.post("/api/v1/auth/login")
async def login(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    identifier = str(body.get("identifier") or body.get("username") or "").strip()
    password = str(body.get("password") or "")
    try:
        token, user = await auth.login(identifier, password)
    except SubsonicError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    return JSONResponse({"token": token, "user": user})


@app.get("/api/v1/auth/me")
async def me(user=Depends(auth.current_user_http)) -> JSONResponse:
    return JSONResponse({"user": user})


@app.post("/api/v1/auth/logout")
async def logout(request: Request) -> JSONResponse:
    token = auth._token_from_request(request)
    if token:
        await db.delete_session(auth.hash_token(token))
    return JSONResponse({"status": "ok"})


# --- Last.fm ----------------------------------------------------------------


@app.get("/api/v1/lastfm/status")
async def lastfm_status(request: Request, user=Depends(auth.current_user_http)) -> JSONResponse:
    return JSONResponse(await request.app.state.services["lastfm"].status(user["user_id"]))


@app.get("/api/v1/lastfm/connect")
async def lastfm_connect(request: Request, user=Depends(auth.current_user_http)) -> JSONResponse:
    service: LastfmService = request.app.state.services["lastfm"]
    if not service.available:
        raise HTTPException(status_code=503, detail="Last.fm API key is not configured")
    url = await service.connect_url(user["user_id"])
    if not url:
        raise HTTPException(status_code=502, detail="Last.fm did not issue a token")
    return JSONResponse({"url": url})


@app.get("/api/v1/lastfm/callback")
async def lastfm_callback(request: Request, token: str = "") -> HTMLResponse:
    service: LastfmService = request.app.state.services["lastfm"]
    user_id = await service.finish(token) if token else None
    ok = user_id is not None
    return_url = settings.lastfm_return_url
    title = "Last.fm connected" if ok else "Last.fm connection failed"
    message = (
        "Your Last.fm account is linked. You can return to the player."
        if ok
        else "The link could not be completed. Start again from the player."
    )
    html = f"""<!doctype html><meta charset="utf-8"><title>{title}</title>
<body style="font-family:system-ui;background:#111;color:#eee;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center;max-width:420px">
<h1 style="font-size:20px">{title}</h1><p style="color:#aaa">{message}</p>
<p><a style="color:#fb5c74" href="{return_url}">Back to the player</a></p>
</div>
<script>setTimeout(function(){{location.href={return_url!r}}},1200)</script>
</body>"""
    return HTMLResponse(html)


@app.post("/api/v1/lastfm/disconnect")
async def lastfm_disconnect(request: Request, user=Depends(auth.current_user_http)) -> JSONResponse:
    service: LastfmService = request.app.state.services["lastfm"]
    return JSONResponse(await service.disconnect(user["user_id"]))


@app.post("/api/v1/lastfm/scrobbling")
async def lastfm_scrobbling(request: Request, user=Depends(auth.current_user_http)) -> JSONResponse:
    enabled = str(request.query_params.get("enabled", "true")).lower() in {"1", "true", "yes"}
    service: LastfmService = request.app.state.services["lastfm"]
    return JSONResponse(await service.set_scrobbling(user["user_id"], enabled))


# --- playback quality -------------------------------------------------------


@app.get("/api/v1/quality")
async def get_quality(request: Request, user=Depends(auth.current_user_http)) -> JSONResponse:
    value = await db.get_quality(user["user_id"])
    return JSONResponse({"quality": value, "options": list(db.QUALITY_VALUES)})


@app.post("/api/v1/quality")
async def set_quality(request: Request, user=Depends(auth.current_user_http)) -> JSONResponse:
    value = request.query_params.get("value") or ""
    if value not in db.QUALITY_VALUES:
        raise HTTPException(status_code=400, detail="Unknown quality")
    saved = await db.set_quality(user["user_id"], value)
    return JSONResponse({"quality": saved, "options": list(db.QUALITY_VALUES)})


@app.get("/api/v1/downloads")
async def downloads(user=Depends(auth.current_user_http)) -> JSONResponse:
    """Downloaded-library metadata imported from the old hify database."""
    return JSONResponse({"downloads": await db.legacy_downloads(user["user_id"])})


# --- static player ----------------------------------------------------------

_build = BASE_DIR / "web" / "dist"
if _build.is_dir():
    app.mount("/", StaticFiles(directory=str(_build), html=True), name="web")
