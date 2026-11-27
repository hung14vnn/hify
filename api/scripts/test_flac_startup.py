from __future__ import annotations

import asyncio
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from config import settings
from providers.flac_bridge import FlacBridge
from providers.streaming import StreamService


class FlacStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        test_settings = replace(
            settings, eclipse_addons=("https://addon.test",),
            eclipse_bearer_token="fixture", eclipse_quality="LOSSLESS",
            allow_private_stream_hosts=True,
        )
        for module in ("providers.flac_bridge", "providers.streaming"):
            self.enterContext(patch(f"{module}.settings", test_settings))
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.provider), trust_env=False)
        self.addAsyncCleanup(self.http.aclose)
        self.bridge = FlacBridge(self.http)
        self.service = StreamService(self.http, None, None, self.bridge)
        self.service.song_meta = AsyncMock(return_value={"title": "Tone", "artist": "Fixture", "duration": 8})
        self.calls = Counter()
        self.held_path = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.reject_stream = False
        self.tone = (Path(__file__).parent / "fixtures" / "test-tone.flac").read_bytes()

    async def provider(self, request):
        path = request.url.path
        self.calls[path] += 1
        if path == self.held_path:
            self.entered.set()
            await self.release.wait()
        if path == "/manifest.json":
            return httpx.Response(200, json={"id": "fixture", "resources": ["resolve", "stream"]})
        if path == "/api/addon-access/token":
            return httpx.Response(200, json={"token": "fixture", "expiresIn": 300})
        if path == "/resolve":
            self.assertEqual(request.headers["X-Eclipse-Access"], "fixture")
            title = request.url.params["title"]
            return httpx.Response(200, json={"item": {
                "id": {"朝": "morning", "夜": "night"}.get(title, "tone"), "title": title, "artist": "Fixture",
            }})
        if path.startswith("/stream/"):
            quality = request.url.params["quality"]
            if self.reject_stream:
                return httpx.Response(503)
            if quality == "HIGH":
                return httpx.Response(200, json={"url": "https://audio.test/tone.m4a", "codec": "m4a"})
            return httpx.Response(200, json={
                "url": f"https://audio.test/{path.removeprefix('/stream/')}.flac", "codec": "flac",
                "bitDepth": 16, "sampleRate": 44100,
            })
        if path == "/tone.flac":
            requested = request.headers.get("Range")
            if requested:
                first, last = map(int, requested.removeprefix("bytes=").split("-"))
                return httpx.Response(206, headers={
                    "Content-Type": "audio/flac", "Content-Range": f"bytes {first}-{last}/{len(self.tone)}",
                }, content=self.tone[first:last + 1])
            return httpx.Response(200, headers={"Content-Type": "audio/flac"}, content=self.tone)
        self.fail(f"Unexpected request to {path}")

    async def test_play_and_probe_join_an_overlapping_warm_with_independent_audio_ranges(self):
        await self.bridge.warm_manifests()
        self.held_path = "/stream/tone"
        warm = asyncio.create_task(self.service.stream("st-tone", "bytes=0-1"))
        await asyncio.wait_for(self.entered.wait(), timeout=5)
        play = asyncio.create_task(self.service.stream("st-tone", None))
        seek = asyncio.create_task(self.service.stream("st-tone", "bytes=23-68"))
        await asyncio.sleep(0)
        self.release.set()
        warmed, audio, ranged = await asyncio.gather(warm, play, seek)

        self.assertEqual(self.calls["/resolve"], 1)
        self.assertEqual(self.calls["/stream/tone"], 1)
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.headers["Content-Type"], "audio/flac")
        self.assertEqual(audio.headers["X-Audio-Bit-Depth"], "16")
        self.assertEqual(audio.headers["X-Audio-Sample-Rate"], "44100")
        self.assertEqual(b"".join([chunk async for chunk in audio.body_iterator]), self.tone)
        self.assertEqual(b"".join([chunk async for chunk in warmed.body_iterator]), self.tone[:2])
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.headers["Content-Range"], f"bytes 23-68/{len(self.tone)}")
        self.assertEqual(b"".join([chunk async for chunk in ranged.body_iterator]), self.tone[23:69])

    async def test_startup_warm_and_first_play_share_manifest_and_token_requests(self):
        for path in ("/manifest.json", "/api/addon-access/token"):
            with self.subTest(path=path):
                self.calls.clear()
                self.bridge = FlacBridge(self.http)
                self.held_path = path
                self.entered.clear()
                self.release.clear()
                first = asyncio.create_task(self.bridge.resolve(artist="Fixture", title="Tone"))
                await asyncio.wait_for(self.entered.wait(), timeout=5)
                warm = asyncio.create_task(self.bridge.warm_manifests())
                await asyncio.sleep(0)
                self.release.set()
                _, stream = await asyncio.gather(warm, first)
                self.assertEqual(stream.content_type, "audio/flac")
                self.assertEqual(self.calls["/manifest.json"], 1)
                self.assertEqual(self.calls["/api/addon-access/token"], 1)

    async def test_cancelled_probe_does_not_restart_or_cancel_the_players_lookup(self):
        self.held_path = "/resolve"
        probe = asyncio.create_task(self.bridge.resolve(artist="Fixture", title="Tone"))
        await asyncio.wait_for(self.entered.wait(), timeout=5)
        play = asyncio.create_task(self.bridge.resolve(artist="Fixture", title="Tone"))
        await asyncio.sleep(0)
        probe.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await probe
        self.release.set()
        self.assertEqual((await play).content_type, "audio/flac")
        self.assertEqual(self.calls["/resolve"], 1)
        self.assertEqual(self.calls["/stream/tone"], 1)

    async def test_concurrent_qualities_keep_their_own_signed_stream(self):
        streams = await asyncio.gather(*(
            self.bridge.resolve(artist="Fixture", title="Tone", quality=quality)
            for quality in ("LOSSLESS", "HIGH", "HI_RES_LOSSLESS")
        ))
        self.assertEqual([stream.content_type for stream in streams], ["audio/flac", "audio/mp4", "audio/flac"])
        self.assertEqual(self.calls["/stream/tone"], 3)

    async def test_distinct_non_latin_titles_keep_their_identity_during_and_after_lookup(self):
        for _ in range(2):
            morning, night = await asyncio.gather(
                self.bridge.resolve(artist="Fixture", title="朝", duration_s=8),
                self.bridge.resolve(artist="Fixture", title="夜", duration_s=8),
            )
            self.assertEqual(morning.url, "https://audio.test/morning.flac")
            self.assertEqual(night.url, "https://audio.test/night.flac")
        self.assertEqual(self.calls["/resolve"], 2)

    async def test_unavailable_signed_stream_can_be_resolved_again(self):
        self.reject_stream = True
        self.assertIsNone(await self.bridge.resolve(artist="Fixture", title="Tone"))
        self.reject_stream = False
        self.assertEqual((await self.bridge.resolve(artist="Fixture", title="Tone")).content_type, "audio/flac")
        self.assertEqual(self.calls["/stream/tone"], 2)


if __name__ == "__main__":
    unittest.main()
