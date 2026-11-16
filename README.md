# hify

FLAC-first music app: the player from the hify fork, a small FastAPI backend, and
accounts from the `netflix-clone` project (`GroupCalendarCompanion`).

Playback order: **Eclipse FLAC bridge → YouTube Music**. Spotify (via `spotapi`) is
the catalog for search, artists and albums. There is no local library, download
engine, karaoke or admin panel.

## Layout

```
api/   FastAPI: Subsonic-compatible /rest, auth, SQLite, providers
web/   SvelteKit player (copied from the hify player, slimmed)
data/  music.db (sessions, likes, playlists, history, Last.fm links)
```

## Run

```bash
# backend (Python 3.12+; the hify venv also works)
cd api
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
uvicorn main:app --host 127.0.0.1 --port 8089

# player (dev, proxies /rest and /api to :8089)
cd web
pnpm install
pnpm dev            # http://127.0.0.1:5180
```

Production: `cd web && pnpm build`; the backend serves `web/dist` at `/` on the same
port, so login and `/rest` share an origin.

## Configuration

Copy `.env.example` to `.env` in the repo root.

- `NETFLIX_DB_PATH` — read-only `data/db.json` of GroupCalendarCompanion. Login
  verifies the scrypt hash and `status == active`; accounts stay owned by that app.
- `ECLIPSE_ADDON_URLS` — comma-separated personal Eclipse Music add-on URLs
  (with or without `/manifest.json`). Several are tried in order; only direct FLAC
  URLs are used (HLS/DASH is rejected because the player is a plain `<audio>`).
- `LASTFM_API_KEY` / `LASTFM_API_SECRET` — Last.fm application credentials from
  <https://www.last.fm/api/account/create>. The callback URL registered there must
  match `LASTFM_CALLBACK_URL` (default `http://127.0.0.1:8089/api/v1/lastfm/callback`).
  Each user links their own account from Profile → Last.fm → Connect.

## Accounts and data

- Sessions live in `data/music.db`; the netflix-clone database is never written.
- Every row is keyed by the `usr_…` id from that database, so a future move to JWT
  or SSO only replaces `api/auth.py`.
- Library = saved artists, liked songs, playlists and play history. Audio is always
  resolved at play time.

## App endpoints (Subsonic subset the player uses)

`ping`, `getUser`, `search3`, `searchSpotifyTracks`, `stream`, `getArtist`,
`getAlbum`, `getArtists`, `getTopSongs`, `getStarred2`, `star`, `unstar`,
`getPlaylists`, `getPlaylist`, `createPlaylist`, `updatePlaylist`,
`deletePlaylist`, `getPlayQueue`, `savePlayQueue`, `getRecentlyPlayedSongs`,
`getTodaysHits`, `getTrendingSongs`, `getRandomSongs`, `getLyrics`, `scrobble`,
`getLastfmStatus`, `setLastfmScrobbling`, `getAlbumList2`, `getScanStatus`.

Custom: `/api/v1/auth/{login,me,logout}`, `/api/v1/lastfm/{status,connect,callback,disconnect,scrobbling}`.

## Diagnostics

```bash
python api/scripts/smoke.py admin admin1   # login, search, stream, likes, playlists, queue
python api/scripts/range_probe.py          # Range behaviour (bytes=0- is bounded for YouTube)
```

To exercise the FLAC path without a real add-on, start the fake add-on and point the
backend at it (it serves an 8 s FLAC through `stream/t1`):

```bash
python api/scripts/fake_addon.py
ECLIPSE_ADDON_URLS=http://127.0.0.1:8090 ALLOW_PRIVATE_STREAM_HOSTS=true \
  uvicorn main:app --port 8089
python api/scripts/range_probe.py          # now answers audio/flac
```

