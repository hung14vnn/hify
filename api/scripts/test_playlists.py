from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

import db
import subsonic


def song(track_id, **fields):
    return {"id": track_id, "title": f"Song {track_id}", **fields}


class PlaylistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "music.db"
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript(db.SCHEMA)
        self.statements = []
        self.connections = 0
        original_connect = db._connect

        @contextmanager
        def connect():
            self.connections += 1
            conn = original_connect(self.path)
            conn.set_trace_callback(self.statements.append)
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

        database = patch.object(db, "_connect", connect)
        database.start()
        self.addCleanup(database.stop)
        self.catalog = {key: song(key) for key in ("a", "b", "c", "new")}
        provider = patch("providers.meta.get_song", side_effect=self.catalog.get)
        self.get_song = provider.start()
        self.addCleanup(provider.stop)
        clock = patch.object(db.time, "time", return_value=200)
        clock.start()
        self.addCleanup(clock.stop)

    def seed(self, playlist_id="p", entries=(), *, user_id="user", updated=100):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(
                "INSERT INTO playlists VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (playlist_id, user_id, f"List {playlist_id}", "Comment", "Owner", 0,
                 "unused-cover", 0, updated),
            )
            conn.executemany(
                "INSERT INTO playlist_items VALUES (?, ?, ?, ?)",
                [(playlist_id, position, item["id"], json.dumps(item))
                 for position, item in entries],
            )

    def stored_items(self, playlist_id="p"):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return conn.execute(
                "SELECT position, track_id, payload FROM playlist_items"
                " WHERE playlist_id = ? ORDER BY position", (playlist_id,),
            ).fetchall()

    def item_writes(self):
        return [sql for sql in self.statements
                if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
                and "playlist_items" in sql.lower()]

    async def listing(self, user_id="user"):
        request = Request({"type": "http", "query_string": b""})
        response = await subsonic.get_playlists(request, {"user_id": user_id})
        return json.loads(response.body)

    async def test_empty_library_returns_empty_playlist_list(self):
        self.assertEqual(await self.listing(), {
            "subsonic-response": {
                "status": "ok", "version": "1.16.1", "playlists": {"playlist": []},
            },
        })

    async def test_listing_preserves_full_response_order_duplicates_and_owner_isolation(self):
        first = song("a", duration="12", coverArt="cover-a", extra={"x": 1})
        second = song("b", duration=None)
        self.seed(entries=[(8, second), (2, first), (11, first)])
        self.seed("empty", updated=150)
        self.seed("other", [(0, song("private"))], user_id="other", updated=190)
        expected = {
            "id": "p", "name": "List p", "comment": "Comment", "owner": "Owner",
            "public": False, "songCount": 3, "duration": 24, "coverArt": "cover-a",
            "created": "1970-01-01T00:00:00+00:00", "changed": "1970-01-01T00:01:40+00:00",
            "entry": [first, second, first],
        }
        empty = {
            **expected, "id": "empty", "name": "List empty", "songCount": 0,
            "duration": 0, "coverArt": None, "changed": "1970-01-01T00:02:30+00:00",
            "entry": [],
        }
        self.assertEqual(await self.listing(), {
            "subsonic-response": {"status": "ok", "version": "1.16.1",
                                  "playlists": {"playlist": [empty, expected]}},
        })
        self.assertIsNone(await db.playlist("other", "user"))

    async def test_listing_uses_bounded_queries_for_many_playlists(self):
        for index in range(12):
            self.seed(str(index), [(0, song(str(index)))], updated=index)
        response = await self.listing()
        selects = [sql for sql in self.statements if sql.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(response["subsonic-response"]["playlists"]["playlist"]), 12)
        self.assertLessEqual(len(selects), 2, selects)
        self.assertLessEqual(self.connections, 2)

    async def test_metadata_update_does_not_read_or_rewrite_items(self):
        self.seed(entries=[(0, song("a")), (7, song("b"))])
        before = self.stored_items()
        await db.update_playlist("p", "user", name="Renamed", comment="", public=True)
        self.assertEqual(self.stored_items(), before)
        self.assertEqual(self.item_writes(), [])
        self.assertFalse(any("playlist_items" in sql.lower() for sql in self.statements))
        full = await db.playlist("p", "user")
        self.assertEqual((full["name"], full["comment"], full["public"], full["updated_at"]),
                         ("Renamed", "", 1, 200))
        self.get_song.assert_not_called()

    async def test_append_inserts_only_new_items_including_one_existing_and_gapped_cases(self):
        for positions in ([], [0], [2, 9]):
            with self.subTest(positions=positions):
                playlist_id = f"p-{len(positions)}"
                self.seed(playlist_id, [(position, song("a")) for position in positions])
                before = self.stored_items(playlist_id)
                self.statements.clear()
                await db.update_playlist(playlist_id, "user", add_ids=["new", "missing", "new"])
                after = self.stored_items(playlist_id)
                start = max(positions, default=-1) + 1
                self.assertEqual(after[:len(before)], before)
                self.assertEqual([(row[0], row[1]) for row in after[len(before):]],
                                 [(start, "new"), (start + 1, "new")])
                writes = self.item_writes()
                self.assertEqual(len(writes), 2, writes)
                self.assertTrue(all(sql.lstrip().upper().startswith("INSERT") for sql in writes))

    async def test_mixed_removals_use_sorted_indexes_and_preserve_duplicates(self):
        self.seed(entries=[(0, song("a")), (3, song("b")), (6, song("a")), (9, song("c"))])
        await db.update_playlist("p", "user", remove_indexes=[1, 1, -1, 99, 3],
                                 add_ids=["missing", "new", "new"], comment="Edited")
        rows = self.stored_items()
        self.assertEqual([(row[0], row[1]) for row in rows],
                         [(0, "a"), (1, "a"), (2, "new"), (3, "new")])
        self.assertEqual([json.loads(row[2]) for row in rows],
                         [song("a"), song("a"), song("new"), song("new")])
        self.assertEqual((await db.playlist("p", "user"))["comment"], "Edited")

    async def test_missing_or_foreign_playlist_update_has_no_effect(self):
        self.seed(entries=[(0, song("a"))], user_id="other")
        before = self.stored_items()
        for playlist_id in ("p", "missing"):
            await db.update_playlist(playlist_id, "user", name="Changed", add_ids=["new"],
                                     remove_indexes=[0])
        self.assertEqual(self.stored_items(), before)
        self.assertEqual(self.item_writes(), [])
        self.get_song.assert_not_called()
        self.assertEqual((await db.playlist("p", "other"))["name"], "List p")

    async def test_failed_append_rolls_back_items_and_metadata(self):
        self.seed(entries=[(0, song("a"))])
        before = await db.playlist("p", "user")
        self.get_song.side_effect = [song("new"), RuntimeError("synthetic lookup failure")]
        with self.assertRaisesRegex(RuntimeError, "synthetic lookup failure"):
            await db.update_playlist("p", "user", name="Changed", add_ids=["new", "broken"])
        self.assertEqual(await db.playlist("p", "user"), before)


if __name__ == "__main__":
    unittest.main()
