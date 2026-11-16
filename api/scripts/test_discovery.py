from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

import subsonic
from errors import SubsonicError


def track(track_id="track-a", album_id="album-a"):
    return {
        "id": track_id,
        "name": f"Song {track_id}",
        "artists": [{"id": "artist-a", "name": "Artist"}],
        "duration_ms": 180000,
        "album": {"id": album_id, "name": "New release", "images": []},
    }


def payload(response):
    return json.loads(response.body)["subsonic-response"]


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        subsonic._DISCOVERY_CACHE.clear()
        subsonic._DISCOVERY_LOCKS = {key: asyncio.Lock() for key in subsonic._DISCOVERY_IDS}
        self.spotify = SimpleNamespace(get_playlist=AsyncMock(side_effect=self.playlist))
        self.ytmusic = SimpleNamespace(get_chart_songs=AsyncMock(return_value=[
            {"videoId": "video-a", "title": "Trending song", "artists": [{"name": "Artist"}]}
        ]))
        self.user = {"user_id": "test-user"}

    async def playlist(self, playlist_id):
        return {"id": playlist_id, "name": "Playlist", "images": [], "_tracks": [track()]}

    def request(self, query=""):
        app = SimpleNamespace(state=SimpleNamespace(services={"spotify": self.spotify, "ytmusic": self.ytmusic}))
        return Request({"type": "http", "query_string": query.encode(), "app": app})

    async def test_hits_and_trending_use_distinct_providers(self):
        hits = payload(await subsonic.todays_hits(self.request("count=12"), self.user))
        trending = payload(await subsonic.trending_songs(self.request("country=us"), self.user))
        self.assertEqual(hits["todaysHits"]["song"][0]["id"], "st-track-a")
        self.assertEqual(trending["trendingSongs"]["song"][0]["id"], "yt-video-a")
        self.spotify.get_playlist.assert_awaited_once_with(subsonic._TODAYS_TOP_HITS)
        self.ytmusic.get_chart_songs.assert_awaited_once_with("US", limit=20)

    async def test_hits_failure_does_not_duplicate_trending(self):
        self.spotify.get_playlist.side_effect = RuntimeError("unavailable")
        hits = payload(await subsonic.todays_hits(self.request(), self.user))
        self.assertEqual(hits["todaysHits"]["song"], [])
        self.ytmusic.get_chart_songs.assert_not_awaited()
        self.assertNotIn(subsonic._TODAYS_TOP_HITS, subsonic._DISCOVERY_CACHE)

    async def test_chart_country_and_count_validation(self):
        await subsonic.trending_songs(self.request("country=invalid&count=999"), self.user)
        self.ytmusic.get_chart_songs.assert_awaited_once_with(subsonic.settings.yt_country, limit=100)

    async def test_popular_playlists_allow_partial_failure(self):
        async def fetch(playlist_id):
            if playlist_id == subsonic._POPULAR_PLAYLISTS[0]:
                raise RuntimeError("unavailable")
            return await self.playlist(playlist_id)
        self.spotify.get_playlist.side_effect = fetch
        result = payload(await subsonic.trending_playlists(self.request(), self.user))
        playlists = result["playlists"]["playlist"]
        self.assertEqual(len(playlists), 7)
        self.assertTrue(all(item["id"].startswith("spotify-playlist-") for item in playlists))
        self.assertTrue(all(item["songCount"] == 1 for item in playlists))

    async def test_discovery_cache_coalesces_and_keeps_tracks(self):
        await asyncio.gather(*(subsonic._discovery_playlist(self.spotify, subsonic._TODAYS_TOP_HITS) for _ in range(3)))
        detail = payload(await subsonic.get_playlist(self.request(f"id=spotify-playlist-{subsonic._TODAYS_TOP_HITS}"), self.user))
        self.assertEqual(detail["playlist"]["entry"][0]["id"], "st-track-a")
        self.spotify.get_playlist.assert_awaited_once()

    async def test_expired_cache_refreshes(self):
        subsonic._DISCOVERY_CACHE[subsonic._TODAYS_TOP_HITS] = (0, {"_tracks": [track("old")]})
        result = await subsonic._discovery_playlist(self.spotify, subsonic._TODAYS_TOP_HITS)
        self.assertEqual(result["_tracks"][0]["id"], "track-a")
        self.spotify.get_playlist.assert_awaited_once()

    async def test_empty_result_is_not_cached(self):
        self.spotify.get_playlist.side_effect = None
        self.spotify.get_playlist.return_value = {"id": subsonic._TODAYS_TOP_HITS, "_tracks": []}
        await subsonic._discovery_playlist(self.spotify, subsonic._TODAYS_TOP_HITS)
        self.assertNotIn(subsonic._TODAYS_TOP_HITS, subsonic._DISCOVERY_CACHE)

    async def test_releases_deduplicate_albums_and_limit(self):
        self.spotify.get_playlist.side_effect = None
        self.spotify.get_playlist.return_value = {
            "id": subsonic._NEW_MUSIC_FRIDAY,
            "_tracks": [track(), track("b"), track("c", "album-b"), track("d", "album-c")],
        }
        result = payload(await subsonic.yt_new_releases(self.request("count=2"), self.user))
        self.assertEqual([album["id"] for album in result["albumList2"]["album"]], ["sl-album-a", "sl-album-b"])
        self.spotify.get_playlist.assert_awaited_once_with(subsonic._NEW_MUSIC_FRIDAY)

    async def test_releases_failure_is_empty(self):
        self.spotify.get_playlist.side_effect = RuntimeError("unavailable")
        result = payload(await subsonic.yt_new_releases(self.request(), self.user))
        self.assertEqual(result["albumList2"]["album"], [])

    async def test_invalid_catalog_id_is_rejected(self):
        with self.assertRaises(SubsonicError):
            await subsonic.get_playlist(self.request("id=spotify-playlist-invalid"), self.user)
        self.spotify.get_playlist.assert_not_awaited()

    async def test_catalog_failure_returns_subsonic_error(self):
        self.spotify.get_playlist.side_effect = RuntimeError("unavailable")
        with self.assertRaisesRegex(SubsonicError, "Spotify playlist is unavailable"):
            await subsonic.get_playlist(self.request(f"id=spotify-playlist-{subsonic._TODAYS_TOP_HITS}"), self.user)

    async def test_playlist_summary_does_not_convert_songs(self):
        playlist = await self.playlist(subsonic._TODAYS_TOP_HITS)
        with patch.object(subsonic, "song_from_spotify") as convert:
            result = subsonic._spotify_playlist_json(playlist)
            convert.assert_not_called()
            self.assertEqual(result["songCount"], 1)
            self.assertEqual(result["duration"], 180)
            self.assertNotIn("entry", result)

    async def test_personal_playlist_retains_owner_scope(self):
        row = {"id": "pl-test", "name": "Mine", "created_at": 1, "updated_at": 1, "entry": []}
        with patch.object(subsonic.db, "playlist", new=AsyncMock(return_value=row)) as lookup:
            result = payload(await subsonic.get_playlist(self.request("id=pl-test"), self.user))
            lookup.assert_awaited_once_with("pl-test", "test-user")
            self.assertEqual(result["playlist"]["name"], "Mine")
        self.spotify.get_playlist.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
