from __future__ import annotations

import asyncio
import json
import math
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import wave
from array import array
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.responses import StreamingResponse
from starlette.requests import Request

import auto_mix
import db
import main
import subsonic
from config import settings
from providers.flac_bridge import FlacBridge, FlacStream
from providers.streaming import StreamService
from providers.ytmusic_stream_service import StreamInfo, YTMusicStreamService


class AutoMixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = self.folder / "music.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(db.SCHEMA)

        self.enterContext(patch.object(db, "settings", replace(settings, music_db_path=path)))
        self.enterContext(patch.object(db, "get_quality", AsyncMock(return_value="LOSSLESS")))
        self.mix = auto_mix.AutoMix()
        self.addAsyncCleanup(self.mix.close)
        self.stream = AsyncMock()
        self.app = SimpleNamespace(state=SimpleNamespace(services={"auto_mix": self.mix, "stream": self.stream}))

    def request(self, query, headers=()):
        return Request({"type": "http", "app": self.app, "query_string": query.encode(), "headers": headers})

    async def cues(self, song_id, token="token", user_id="u"):
        response = await subsonic.get_auto_mix_info(self.request("id=" + song_id + "&mixToken=" + token), {"user_id": user_id})
        return json.loads(response.body)["subsonic-response"]["autoMixInfo"]

    async def play(self, song_id, audio, query="&autoMix=1", request_headers=(), status=200,
                   token="token", recording_key="fixture:track", **headers):
        async def chunks():
            for start in range(0, len(audio), 32768):
                yield audio[start:start + 32768]
        def respond(*args, recording=None, **kwargs):
            if recording is not None:
                recording.observe(recording_key)
            return StreamingResponse(chunks(), status_code=status, headers=headers)
        self.stream.stream.side_effect = respond
        request = self.request("id=" + song_id + "&mixToken=" + token + query, request_headers)
        response = await subsonic.stream(request, {"user_id": "u"})
        return response.body_iterator

    async def settle(self):
        await asyncio.gather(*(task for _, task in self.mix._active.values()))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires FFmpeg")
    async def test_one_flac_delivery_produces_measured_edges_reused_across_service_instances(self):
        wav = self.folder / "tone.wav"
        flac = self.folder / "tone.flac"
        with wave.open(str(wav), "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", ""))
            for seconds, volume in ((3, 300), (30, 14000), (7, 300)):
                tone = array("h", (round(volume * math.sin(i * math.tau / 40)) for i in range(8000)))
                output.writeframes(tone.tobytes() * seconds)
        await asyncio.to_thread(subprocess.run, ["ffmpeg", "-nostdin", "-v", "error", "-i", str(wav), str(flac)], check=True)
        audio = flac.read_bytes()
        song = "st-tone"
        self.assertIsNone(await self.cues(song))
        upstream = AsyncMock(return_value=httpx.Response(200, content=audio, headers={"Content-Type": "audio/flac"}))
        http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(http.aclose)
        bridge = AsyncMock()
        bridge.resolve.return_value = FlacStream("http://fixture/tone", "audio/flac", 40, "fixture", recording_key="fixture:track")
        service = StreamService(http, None, None, bridge)
        service.song_meta = AsyncMock(return_value={"title": "Tone", "artist": "Fixture", "duration": 40})
        self.app.state.services["stream"] = service
        self.enterContext(patch("providers.streaming.settings", replace(settings, allow_private_stream_hosts=True)))
        request = self.request("id=" + song + "&autoMix=1&mixToken=token")
        response = await subsonic.stream(request, {"user_id": "u"})
        self.assertEqual(response.headers["content-type"], "audio/flac")
        stream = response.body_iterator
        first = await anext(stream)
        self.assertTrue(first.startswith(b"fLaC"))
        self.assertFalse(self.mix._active, "First bytes must be independent of the analyzer")
        self.assertEqual(first + b"".join([part async for part in stream]), audio)
        await self.settle()
        upstream.assert_awaited_once()
        expected = {"introSeconds": 3.0, "outroSeconds": 7.0}
        self.assertEqual(await self.cues(song), expected)
        self.assertIsNone(await self.cues("ytv-other"))

        await self.mix.close()
        self.mix = auto_mix.AutoMix()
        self.app.state.services["auto_mix"] = self.mix
        self.app.state.services["stream"] = self.stream
        self.addAsyncCleanup(self.mix.close)
        self.assertIsNone(await self.cues(song), "A new process must first observe this recording again")
        with patch("auto_mix._measure", side_effect=AssertionError("duplicate analysis")) as measure:
            replay = await self.play(song, audio, status=206, request_headers=[(b"range", b"bytes=0-")],
                                     **{"Content-Range": f"bytes 0-{len(audio)-1}/{len(audio)}"})
            self.assertEqual(b"".join([part async for part in replay]), audio)
            await self.settle()
            measure.assert_not_called()
        self.assertEqual(await self.cues(song), expected)
        self.assertIsNone(db.auto_mix_info("fixture:track", auto_mix.VERSION + 1))

    async def test_opt_out_probes_seek_size_limit_and_interrupted_streams_publish_nothing(self):
        audio = b"audio" * 9000
        cases = [
            ("", (), 200, {}),
            ("&autoMix=1", [(b"range", b"bytes=0-1")], 206, {"Content-Range": "bytes 0-1/500"}),
            ("&autoMix=1", [(b"range", b"bytes=0-")], 206, {"Content-Range": "bytes 0-19/500"}),
            ("&autoMix=1&timeOffset=5", (), 200, {}),
            ("&autoMix=1", (), 200, {"Content-Length": str(auto_mix._MAX_BYTES + 1)}),
        ]
        with patch.object(self.mix, "_start", wraps=self.mix._start) as start:
            for query, req, status, headers in cases:
                stream = await self.play("st-a", audio, query, req, status, **headers)
                self.assertEqual(b"".join([part async for part in stream]), audio)
            start.assert_not_called()
        with patch("auto_mix._measure") as measure:
            stream = await self.play("st-a", audio)
            await anext(stream)
            await anext(stream)
            await stream.aclose()
            await self.settle()
            truncated = await self.play("st-a", audio, **{"Content-Length": str(len(audio) + 1)})
            self.assertEqual(b"".join([part async for part in truncated]), audio)
            await self.settle()
            measure.assert_not_called()
        self.assertIsNone(await self.cues("st-a"))

    async def test_same_catalog_id_binds_to_actual_flac_or_youtube_recording_per_play(self):
        def upstream(request):
            if request.url.path == "/stream/studio":
                return httpx.Response(200, json={"url": "http://audio.test/flac?signature=fixture", "codec": "flac"})
            return httpx.Response(200, content=b"studio" if request.url.path == "/flac" else b"live")

        http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        self.addAsyncCleanup(http.aclose)
        bridge = FlacBridge(http)
        studio = await bridge._stream_url("http://addon.test", "studio", "LOSSLESS")
        self.assertRegex(studio.recording_key, r"^flac:[0-9a-f]{64}$")
        bridge.resolve = AsyncMock(side_effect=[studio, None])
        youtube = YTMusicStreamService(http)
        # The original music video was replaced by another upload during resolution.
        resolved = StreamInfo("original-ID", "Live", "Band", 180, None, "http://audio.test/live", "audio/webm",
                              source_video_id="liveVideoID")
        youtube.search = AsyncMock(return_value=resolved)
        youtube._resolve = AsyncMock(return_value=resolved)
        service = StreamService(http, None, youtube, bridge)
        service.song_meta = AsyncMock(return_value={"title": "Song", "artist": "Band", "duration": 180})
        self.app.state.services["stream"] = service
        self.enterContext(patch("providers.streaming.settings", replace(settings, allow_private_stream_hosts=True)))
        with patch("auto_mix._measure", side_effect=lambda path: (3, 7) if path.read_bytes() == b"studio" else (8, 2)):
            for token in ("first", "second"):
                response = await subsonic.stream(self.request("id=st-same&autoMix=1&mixToken=" + token), {"user_id": "u"})
                self.assertEqual(b"".join([part async for part in response.body_iterator]), b"studio" if token == "first" else b"live")
                await self.settle()
        self.assertEqual(await self.cues("st-same", "first"), {"introSeconds": 3, "outroSeconds": 7})
        self.assertEqual(await self.cues("st-same", "second"), {"introSeconds": 8, "outroSeconds": 2})
        self.assertEqual(db.auto_mix_info("yt:liveVideoID", auto_mix.VERSION), {"introSeconds": 8, "outroSeconds": 2})
        self.assertIsNone(await self.cues("st-same", "missing"))
        self.assertIsNone(await self.cues("st-other", "first"))
        self.assertIsNone(await self.cues("st-same", "first", "other-user"))
        with patch("auto_mix.time") as clock:
            clock.monotonic.return_value = float("inf")
            self.assertIsNone(await self.cues("st-same", "first"))
        self.app.state.services["stream"] = self.stream
        conflict = await self.play("st-same", b"live", token="first", recording_key="yt:liveVideoID")
        self.assertEqual(b"".join([part async for part in conflict]), b"live")
        self.assertIsNone(await self.cues("st-same", "first"))

    async def test_workers_deduplicate_bound_parallelism_and_drop_capture_on_pressure(self):
        with patch("auto_mix._measure", return_value=(0.0, 0.0)) as measure:
            first = self.mix._start("st-a")
            self.assertIsNotNone(first)
            self.assertIsNone(self.mix._start("st-a"))
            other = self.mix._start("st-b")
            self.assertIsNotNone(other)
            self.assertIsNone(self.mix._start("st-c"))
            first.offer(b"audio")
            first.offer(None)
            other.cancel()
            await self.settle()
            measure.assert_called_once()
        self.assertEqual(db.auto_mix_info("st-a", auto_mix.VERSION), {"introSeconds": 0.0, "outroSeconds": 0.0})
        self.assertIsNone(db.auto_mix_info("st-b", auto_mix.VERSION))
        stalled = auto_mix._Capture()
        for _ in range(stalled.chunks.maxsize + 1):
            stalled.offer(b"bytes")
        self.assertTrue(stalled.stopped.is_set())

    async def test_cue_lookup_uses_existing_authentication(self):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
            response = await client.get("/rest/getAutoMixInfo?id=st-tone")
        envelope = response.json()["subsonic-response"]
        self.assertEqual((envelope["status"], envelope["error"]["code"]), ("failed", 40))

    def test_cue_reads_and_writes_close_every_sqlite_handle(self):
        connections = []
        original = db._connect
        def connect():
            conn = original()
            connections.append(conn)
            return conn
        with patch.object(db, "_connect", connect):
            db.save_auto_mix_info("fixture:key", 1, 3, 5)
            self.assertEqual(db.auto_mix_info("fixture:key", 1), {"introSeconds": 3, "outroSeconds": 5})
        self.assertEqual(len(connections), 2)
        for conn in connections:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                conn.execute("SELECT 1")

    async def test_steady_or_inaudible_edges_keep_fallback(self):
        for volume in (0, 20, 8000):
            raw = array("h", [volume, -volume] * 20000).tobytes()
            self.assertEqual(auto_mix._edge_seconds(raw, False), 0)
            self.assertEqual(auto_mix._edge_seconds(raw, True), 0)


if __name__ == "__main__":
    unittest.main()
