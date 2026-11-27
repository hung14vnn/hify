from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from collections import Counter, OrderedDict
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from starlette.requests import Request

import db
import subsonic
from config import settings
from providers import meta
from providers.flac_bridge import FlacBridge
from providers.streaming import StreamService


class SavedPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "fixture.db"
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(db.SCHEMA)
        original_connect = db._connect
        self.sql = []

        @contextmanager
        def connect():
            with closing(original_connect(self.path)) as conn, conn:
                conn.set_trace_callback(self.sql.append)
                yield conn

        self.enterContext(patch.object(db, "_connect", connect))
        test_settings = replace(
            settings, music_db_path=self.path, eclipse_addons=("https://addon.test",),
            eclipse_bearer_token="", eclipse_quality="LOSSLESS", allow_private_stream_hosts=True,
        )
        for module in ("db", "providers.streaming", "providers.flac_bridge"):
            self.enterContext(patch(f"{module}.settings", test_settings))
        self.enterContext(patch.object(meta, "_songs", OrderedDict()))
        self.enterContext(patch.object(meta, "_saved_songs", OrderedDict()))
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.addon), trust_env=False)
        self.addAsyncCleanup(self.http.aclose)
        self.calls = Counter()
        self.tone = (Path(__file__).parent / "fixtures" / "test-tone.flac").read_bytes()
        self.spotify = AsyncMock()
        self.youtube = AsyncMock()
        self.service = StreamService(self.http, self.spotify, self.youtube, FlacBridge(self.http))
        self.app = SimpleNamespace(state=SimpleNamespace(services={"stream": self.service}))
        self.owner = {"user_id": "owner", "username": "Listener"}
        self.song = {
            "id": "st-" + uuid4().hex[:22], "title": "Tone", "artist": "Fixture", "duration": 8,
            "artists": [{"id": "sa-band", "name": "Fixture"}], "album": "Test",
            "suffix": "flac", "contentType": "audio/flac", "type": "music", "isDir": False,
        }
        self.spotify.get_track.return_value = {
            "id": self.song["id"][3:], "name": "Tone", "artists": [{"name": "Fixture", "id": "band"}],
            "duration_ms": 8000,
        }

    async def addon(self, request):
        path = request.url.path
        self.calls[path] += 1
        if path == "/manifest.json":
            return httpx.Response(200, json={"id": "fixture", "resources": ["resolve", "stream"]})
        if path == "/resolve":
            self.assertEqual(request.url.params["title"], "Tone")
            self.assertEqual(request.url.params["artist"], "Fixture")
            return httpx.Response(200, json={"item": {"id": "tone", "title": "Tone", "artist": "Fixture"}})
        if path == "/stream/tone":
            self.assertEqual(request.url.params["quality"], "LOSSLESS")
            return httpx.Response(200, json={"url": "https://audio.test/tone.flac", "codec": "flac"})
        if path == "/tone.flac":
            return httpx.Response(200, headers={"Content-Type": "audio/flac"}, content=self.tone)
        self.fail(f"Unexpected path {path}")

    def request(self, query=""):
        return Request({"type": "http", "app": self.app, "query_string": query.encode(), "headers": []})

    async def first_audio(self):
        self.assertFalse(self.calls, "Audio and bridge must be cold at click")
        response = await subsonic.stream(self.request("id=" + self.song["id"]), self.owner)
        stream = response.body_iterator
        try:
            first = await anext(stream)
            self.assertTrue(first.startswith(b"fLaC"))
            self.assertEqual(response.headers["content-type"], "audio/flac")
            self.assertEqual(first + b"".join([part async for part in stream]), self.tone)
        finally:
            await stream.aclose()
        self.assertEqual(self.calls, {"/manifest.json": 1, "/resolve": 1, "/stream/tone": 1, "/tone.flac": 1})

    async def read_favorite(self):
        await db.upsert_star("song", "owner", self.song["id"], self.song)
        response = await subsonic.get_starred2(self.request(), self.owner)
        return json.loads(response.body)["subsonic-response"]["starred2"]["song"][0]

    async def test_saved_favorite_starts_cold_flac_without_spotify(self):
        self.assertIn("starred", await self.read_favorite())
        await self.first_audio()
        self.spotify.get_track.assert_not_awaited()

    async def test_direct_catalog_id_keeps_normal_lookup(self):
        await self.first_audio()
        self.spotify.get_track.assert_awaited_once_with(self.song["id"][3:])

    async def test_saved_youtube_queue_starts_cold_flac_without_catalog_requests(self):
        self.song["id"] = "yt-_Zabcdefgh0"
        self.song.update(suffix="opus", contentType="audio/ogg")
        meta.remember(self.song)
        await db.save_queue("owner", [self.song["id"]], self.song["id"], 53)
        meta._songs.clear()
        await subsonic.get_play_queue(self.request(), self.owner)
        await self.first_audio()
        self.youtube.track_meta.assert_not_awaited()
        self.spotify.get_track.assert_not_awaited()

    async def test_owner_snapshot_keeps_catalog_fields_when_saving_queue_playlist_and_history(self):
        self.song["artists"][0]["private"] = "owner annotation"
        self.song.update(userRating=5, privateNote="owner note", isrc=["fixture"])
        row = await self.read_favorite()
        self.assertEqual(row["userRating"], 5)
        song_id = self.song["id"]
        self.assertIsNone(meta.get_library_song(song_id, "other"))
        self.assertIsNone(meta.get_song(song_id))
        expected = {k: v for k, v in self.song.items() if k not in ("userRating", "privateNote")}
        expected["artists"] = [{"id": "sa-band", "name": "Fixture"}]

        await subsonic.save_play_queue(self.request("id=" + song_id), self.owner)
        self.assertEqual((await db.get_queue("owner"))["entry"], [expected])
        playlist_id = await db.create_playlist(self.owner, "Playlist", [song_id])
        self.assertEqual((await db.playlist(playlist_id, "owner"))["entry"], [expected])
        self.app.state.services["lastfm"] = AsyncMock()
        await subsonic.scrobble(self.request("submission=true&id=" + song_id), self.owner)
        self.assertEqual(self.spotify.get_track.await_count, 0)
        with closing(sqlite3.connect(self.path)) as conn:
            history = json.loads(conn.execute("SELECT payload FROM play_history").fetchone()[0])
        self.assertEqual(history, expected)

    async def test_live_catalog_metadata_has_priority_over_owner_snapshot(self):
        self.song["title"] = "Older imported title"
        await self.read_favorite()
        song_id = self.song["id"]
        self.assertEqual((await self.service.song_meta(song_id, "owner"))["title"], "Older imported title")
        other_song = await self.service.song_meta(song_id, "other")
        self.assertEqual(other_song["title"], "Tone")
        self.assertNotIn("starred", other_song)
        self.assertEqual((await self.service.song_meta(song_id, "owner"))["title"], "Tone")
        self.spotify.get_track.assert_awaited_once_with(song_id[3:])

    async def test_recent_history_restores_catalog_data_without_played_annotation(self):
        await db.history_add("owner", self.song)
        response = await subsonic.recently_played(self.request(), self.owner)
        self.assertIn("played", json.loads(response.body)["subsonic-response"]["recentlyPlayedSongs"]["song"][0])
        self.assertEqual(await self.service.song_meta(self.song["id"], "owner"), self.song)
        self.spotify.get_track.assert_not_awaited()

    async def test_playlist_cards_remember_first_track_and_open_playlist_remembers_other_tracks(self):
        second = dict(self.song, id="st-" + uuid4().hex[:22], title="Second")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT INTO playlists (id, user_id, name, owner, created_at, updated_at) "
                "VALUES ('pl-test', 'owner', 'Test', 'Listener', 0, 0)"
            )
            conn.executemany(
                "INSERT INTO playlist_items VALUES ('pl-test', ?, ?, ?)",
                [(i, song["id"], json.dumps(song)) for i, song in enumerate([self.song, second])],
            )
        await subsonic.get_playlists(self.request(), self.owner)
        self.assertEqual(meta.get_library_song(self.song["id"], "owner"), self.song)
        self.assertIsNone(meta.get_library_song(second["id"], "owner"))
        await self.first_audio()
        # Saving from the card after playback never opens the playlist detail.
        third = dict(self.song, id="st-" + uuid4().hex[:22], title="Other listener")
        await db.upsert_star("song", "other", third["id"], third)
        await subsonic.save_play_queue(self.request(
            f"id={self.song['id']}&id={second['id']}&id={third['id']}&id={second['id']}&position=3"
        ), self.owner)
        queue = await db.get_queue("owner")
        self.assertEqual(queue["entry"], [self.song, second, second])
        self.assertEqual(queue["position"], 3)
        await subsonic.get_playlist(self.request("id=pl-test"), self.owner)
        self.assertEqual(await self.service.song_meta(second["id"], "owner"), second)
        self.spotify.get_track.assert_not_awaited()

    async def test_optional_catalog_fields_follow_their_schema_on_queue_round_trip(self):
        self.song.update(track=True, year="2000", suffix=123, coverArt=True, isDir=0,
                         artistId=[], discNumber=1, artists=[False, {"id": 1, "name": "Fixture"}],
                         isrc=[False, "code", None])
        await self.read_favorite()
        clean = await self.service.song_meta(self.song["id"], "owner")
        for field in ("track", "year", "suffix", "coverArt", "isDir", "artistId"):
            self.assertNotIn(field, clean)
        self.assertEqual(clean["artists"], [{"name": "Fixture"}])
        self.assertEqual(clean["isrc"], ["code"])
        self.assertEqual(clean["discNumber"], 1)
        await subsonic.save_play_queue(self.request("id=" + self.song["id"]), self.owner)
        result = await subsonic.get_play_queue(self.request(), self.owner)
        self.assertEqual(json.loads(result.body)["subsonic-response"]["playQueue"]["entry"], [clean])

    async def test_queue_save_recovers_evicted_entries_from_owners_queue_favorites_and_history_in_one_query(self):
        previous = dict(self.song, id="ytv-_Zabcdefgh0", title="Video")
        favorite = dict(self.song, id="st-" + uuid4().hex[:22], title="Liked")
        history = dict(self.song, id="st-" + uuid4().hex[:22], title="Played")
        foreign = dict(self.song, id="st-" + uuid4().hex[:22], title="Private to another account")
        meta.remember(previous)
        await db.save_queue("owner", [previous["id"]], previous["id"], 42)
        meta._songs.clear()
        await db.upsert_star("song", "owner", favorite["id"], dict(favorite, starred="old timestamp"))
        await db.history_add("owner", dict(history, played="old timestamp"))
        await db.history_add("other", foreign)
        ids = [history["id"], favorite["id"], previous["id"], foreign["id"], favorite["id"]]
        ids += [f"st-{n:022}" for n in range(1100)]
        self.sql.clear()
        await db.save_queue("owner", ids, favorite["id"], 12)
        lookups = [sql for sql in self.sql if sql.lstrip().upper().startswith(("WITH", "SELECT"))]
        self.assertEqual(len(lookups), 1)
        self.assertEqual(await db.get_queue("owner"), {
            "entry": [history, favorite, previous, favorite], "current": favorite["id"], "position": 12,
        })
        self.spotify.get_track.assert_not_awaited()
        self.youtube.track_meta.assert_not_awaited()

    async def test_incomplete_and_placeholder_entries_keep_remote_fallback(self):
        song_id = self.song["id"]
        for fields in ({"artist": "Unknown Artist"}, {"title": song_id}, {"duration": "8"},
                       {"type": "album"}, {"title": ""}, {"id": "yt-bad"}):
            with self.subTest(fields=fields):
                song = dict(self.song, **fields)
                meta.remember_library_songs("owner", [song])
                self.assertIsNone(meta.get_library_song(song["id"], "owner"))
        await self.first_audio()
        self.spotify.get_track.assert_awaited_once_with(song_id[3:])

    async def test_direct_queue_resave_preserves_display_placeholders_but_playback_fetches_metadata(self):
        untitled = dict(self.song, title="Untitled")
        unknown_artist = dict(self.song, id="st-" + uuid4().hex[:22], artist="Unknown Artist")
        without_duration = dict(self.song, id="st-" + uuid4().hex[:22])
        del without_duration["duration"]
        stored = [dict(untitled, suffix=123, privateNote="omit"), unknown_artist, without_duration]
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO play_queue VALUES ('owner', ?, ?, 0, 0)",
                         (json.dumps(stored), untitled["id"]))
        del untitled["suffix"]
        wanted = [unknown_artist, untitled, without_duration, untitled]
        query = "&".join("id=" + song["id"] for song in wanted)
        await subsonic.save_play_queue(self.request(query), self.owner)
        response = await subsonic.get_play_queue(self.request(), self.owner)
        self.assertEqual(json.loads(response.body)["subsonic-response"]["playQueue"]["entry"], wanted)
        self.spotify.get_track.assert_not_awaited()
        for song in (untitled, unknown_artist, without_duration):
            self.spotify.get_track.return_value["id"] = song["id"][3:]
            resolved = await self.service.song_meta(song["id"], "owner")
            self.assertEqual((resolved["title"], resolved["artist"], resolved["duration"]), ("Tone", "Fixture", 8))
            self.spotify.get_track.assert_awaited_with(song["id"][3:])
        self.assertEqual(self.spotify.get_track.await_count, 3)

    async def test_stream_joins_cold_now_playing_lookup_and_survives_its_cancellation(self):
        self.app.state.services["lastfm"] = AsyncMock()
        entered, joined, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        song_id = self.song["id"]

        async def get_track(track_id):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return self.spotify.get_track.return_value

        original_meta = self.service.song_meta

        async def song_meta(track_id, user_id):
            if entered.is_set():
                joined.set()
            return await original_meta(track_id, user_id)

        self.spotify.get_track.side_effect = get_track
        self.service.song_meta = song_meta
        scrobble = asyncio.create_task(subsonic.scrobble(self.request("id=" + song_id), self.owner))
        await asyncio.wait_for(entered.wait(), 5)
        play = asyncio.create_task(self.first_audio())
        await asyncio.wait_for(joined.wait(), 5)
        scrobble.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await scrobble
        release.set()
        await asyncio.wait_for(play, 5)
        self.spotify.get_track.assert_awaited_once_with(song_id[3:])

    async def test_catalog_miss_can_retry_and_similar_ids_keep_their_identity(self):
        first, second = "x" * 22, "X" * 22
        self.spotify.get_track.side_effect = RuntimeError("fixture failure")
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            await self.service.song_meta("st-" + first)

        async def get_track(track_id):
            return dict(self.spotify.get_track.return_value, id=track_id, name=track_id)

        self.spotify.get_track.side_effect = get_track
        tracks = await asyncio.gather(*[self.service.song_meta("st-" + id_) for id_ in (first, second)])
        self.assertEqual([song["title"] for song in tracks], [first, second])
        self.assertEqual(self.spotify.get_track.await_count, 3)


if __name__ == "__main__":
    unittest.main()
