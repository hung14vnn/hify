# hify

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

Production: `cd web && pnpm build`. The committed `web/.env.production` points the
player at `https://music.hungtp.ninja/hify-api`; override it with
`PUBLIC_API_BASE=https://api.example.com pnpm build` when hosting elsewhere.
The backend can also serve `web/dist` at `/` on the same origin as `/rest`.

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
- `OUTBOUND_PROXY` — optional HTTP CONNECT proxy for YouTube. On a datacenter IP the
  API can point this at a proxy running inside a VPN namespace so yt-dlp and the
  audio proxy both leave from the VPN address.

## Accounts and data

- Sessions live in `data/music.db`; the netflix-clone database is never written.
- Every row is keyed by the `usr_…` id from that database, so a future move to JWT
  or SSO only replaces `api/auth.py`.
- Accounts imported from the old hify database live in `legacy_users` and verify
  their original bcrypt hashes, so the old username/password keeps working.
- Library = saved artists, liked songs, playlists and play history. Audio is always
  resolved at play time.

## Migrating the old hify library

`api/scripts/migrate_hify.py` imports users, playlists and downloaded-library
metadata from the old `cache/library.db`. Local tracks are resolved to Spotify or
YouTube Music ids; a per-user `Downloaded (old hify)` playlist holds the shared
library, and the `legacy_downloads` table keeps file paths and quality data.

```bash
cd api
python scripts/migrate_hify.py --old-db ../legacy/library.db --dry-run
python scripts/migrate_hify.py --old-db ../legacy/library.db
```

The script snapshots the old database, caches resolution results and writes a JSON
report with unmatched tracks. Re-running replaces the imported playlists.

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

