from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from starlette.requests import Request

import subsonic
from errors import SubsonicError
from providers.meta import get_song
from providers.streaming import StreamService
from providers.ytmusic_stream_service import StreamInfo, YTMusicStreamService, _YtDlp


VIDEO_ID = "abcdefghijk"


class YouTubeSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.video = {
            "id": VIDEO_ID, "title": "Acoustic session (Official Video)",
            "duration": 215, "channel": "Independent channel",
            "thumbnails": [{"url": "https://images.test/video.jpg"}],
        }
        self.ytmusic = SimpleNamespace(
            search_videos=AsyncMock(return_value=[self.video]),
            search_tracks=AsyncMock(return_value=[]),
        )
        self.spotify = SimpleNamespace(search_all=AsyncMock())

    def request(self, query):
        app = SimpleNamespace(state=SimpleNamespace(services={
            "ytmusic": self.ytmusic, "spotify": self.spotify,
        }))
        return Request({"type": "http", "query_string": query.encode(), "app": app})

    async def test_youtube_is_separate_and_returns_playable_metadata(self):
        result = await subsonic.search3(self.request("query=acoustic&source=youtube&songCount=10"), {})
        payload = json.loads(result.body)["subsonic-response"]["searchResult3"]
        song = payload["song"][0]
        self.assertEqual(song["id"], f"ytv-{VIDEO_ID}")
        self.assertEqual(song["title"], self.video["title"])
        self.assertEqual(song["artist"], self.video["channel"])
        self.assertEqual(song["duration"], 215)
        self.assertEqual(song["coverArt"], self.video["thumbnails"][0]["url"])
        self.assertEqual(get_song(song["id"])["title"], song["title"])
        self.ytmusic.search_videos.assert_awaited_once_with("acoustic", limit=10)
        self.ytmusic.search_tracks.assert_not_awaited()
        self.spotify.search_all.assert_not_awaited()

    async def test_ytmusic_still_uses_music_catalog(self):
        await subsonic.search3(self.request("query=song&source=ytmusic&songCount=10"), {})
        self.ytmusic.search_tracks.assert_awaited_once_with("song", limit=10)
        self.ytmusic.search_videos.assert_not_awaited()

    async def test_empty_and_zero_count_skip_remote_search(self):
        for query in ("source=youtube", "query=song&source=youtube&songCount=0"):
            await subsonic.search3(self.request(query), {})
        self.ytmusic.search_videos.assert_not_awaited()

    async def test_video_result_count_is_bounded(self):
        await subsonic.search3(self.request("query=song&source=youtube&songCount=999&songOffset=-5"), {})
        self.ytmusic.search_videos.assert_awaited_once_with("song", limit=50)

    async def test_offset_selects_requested_page(self):
        self.ytmusic.search_videos.return_value = [dict(self.video, id="00000000000"), self.video]
        result = await subsonic.search3(self.request("query=song&source=youtube&songCount=1&songOffset=1"), {})
        songs = json.loads(result.body)["subsonic-response"]["searchResult3"]["song"]
        self.assertEqual([song["id"] for song in songs], [f"ytv-{VIDEO_ID}"])
        self.ytmusic.search_videos.assert_awaited_once_with("song", limit=2)

    async def test_youtube_playback_bypasses_flac_and_music_matching(self):
        service = StreamService(MagicMock(), self.spotify, self.ytmusic, MagicMock())
        service._proxy_yt = AsyncMock(return_value="audio")
        service.song_meta = AsyncMock()
        result = await service.stream(f"ytv-{VIDEO_ID}", "bytes=0-1", "user")
        self.assertEqual(result, "audio")
        service._proxy_yt.assert_awaited_once_with(f"ytv-{VIDEO_ID}", "bytes=0-1")
        service.song_meta.assert_not_awaited()
        service._flac.resolve.assert_not_called()

    async def test_invalid_video_id_is_not_extracted(self):
        service = StreamService(MagicMock(), self.spotify, self.ytmusic, MagicMock())
        service._proxy_yt = AsyncMock()
        with self.assertRaises(SubsonicError):
            await service.stream("ytv-bad/id", None)
        service._proxy_yt.assert_not_awaited()

    async def test_video_metadata_can_be_rebuilt_without_music_catalog(self):
        raw = {"videoId": "zyxwvutsrqp", "title": "Rare upload", "artists": [{"name": "Uploader"}]}
        self.ytmusic.video_meta = AsyncMock(return_value=raw)
        service = StreamService(MagicMock(), self.spotify, self.ytmusic, MagicMock())
        song = await service.song_meta("ytv-zyxwvutsrqp")
        self.assertEqual(song["id"], "ytv-zyxwvutsrqp")
        self.assertEqual(song["artist"], "Uploader")
        self.ytmusic.video_meta.assert_awaited_once_with("zyxwvutsrqp")

    async def test_stream_selects_m4a_for_ios_user_agent(self):
        service = StreamService(MagicMock(), self.spotify, self.ytmusic, MagicMock())
        service._proxy_yt = AsyncMock(return_value="audio")
        service.song_meta = AsyncMock()
        ios_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15"
        result = await service.stream(f"ytv-{VIDEO_ID}", "bytes=0-1", "user", user_agent=ios_ua)
        self.assertEqual(result, "audio")
        service._proxy_yt.assert_awaited_once_with(f"ytv-{VIDEO_ID}", "bytes=0-1", fmt="m4a")

    async def test_download_selects_m4a_for_ios_user_agent(self):
        service = StreamService(MagicMock(), self.spotify, self.ytmusic, MagicMock())
        mock_resp = MagicMock()
        mock_resp.headers = {"Content-Type": "audio/mp4"}
        service._proxy_yt = AsyncMock(return_value=mock_resp)
        ios_ua = "AppleCoreMedia/1.0.0.21E236 (iPad; U; CPU OS 17_4_1 like Mac OS X)"
        await service.download(f"ytv-{VIDEO_ID}", None, user_agent=ios_ua)
        service._proxy_yt.assert_awaited_once_with(f"ytv-{VIDEO_ID}", None, fmt="m4a")
        self.assertIn(".m4a", mock_resp.headers["Content-Disposition"])


class YouTubeExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = httpx.AsyncClient(trust_env=False)
        self.addAsyncCleanup(self.http.aclose)
        self.service = YTMusicStreamService(self.http)
        self.info = StreamInfo(
            video_id=VIDEO_ID, title="Song (Official Video)", artist="Channel",
            duration_s=215, thumbnail="https://images.test/video.jpg",
            audio_url="https://audio.test/stream", content_type="audio/webm",
            source_title="Song (Official Video)", source_artist="Channel",
        )
        self.service._run_blocking = AsyncMock(return_value=self.info)
        self.service._upgrade_mv_to_audio = AsyncMock(return_value=self.info)

    async def test_original_video_has_separate_cache_and_no_studio_replacement(self):
        original = await self.service._resolve(f"ytv-{VIDEO_ID}", None, None, "opus")
        self.assertEqual(original.video_id, f"ytv-{VIDEO_ID}")
        self.service._run_blocking.assert_awaited_once_with(_YtDlp.extract_video, VIDEO_ID, "opus", what="extraction")
        self.service._upgrade_mv_to_audio.assert_not_awaited()
        await self.service._resolve(f"ytv-{VIDEO_ID}", None, None, "opus")
        self.assertEqual(self.service._run_blocking.await_count, 1)
        await self.service._resolve(VIDEO_ID, None, None, "opus")
        self.assertEqual(self.service._run_blocking.await_count, 2)
        self.service._upgrade_mv_to_audio.assert_awaited_once()

    async def test_general_video_metadata_reuses_original_stream_cache(self):
        raw = await self.service.video_meta(VIDEO_ID)
        self.assertEqual(raw["videoId"], VIDEO_ID)
        self.assertEqual(raw["title"], self.info.title)
        self.assertEqual(raw["duration_seconds"], 215)
        self.service._upgrade_mv_to_audio.assert_not_awaited()

    def test_flat_search_preserves_video_channel_thumbnail(self):
        with patch("providers.ytmusic_stream_service.YoutubeDL") as factory:
            ydl = factory.return_value.__enter__.return_value
            ydl.extract_info.return_value = {"entries": [None, {
                "id": VIDEO_ID, "title": "Rare upload", "uploader": "Uploader",
                "duration": 90, "thumbnails": [{"url": "https://images.test/cover.jpg"}],
            }]}
            videos = _YtDlp.search_videos("rare upload", 10)
            ydl.extract_info.assert_called_once_with("ytsearch10:rare upload", download=False)
            self.assertEqual(videos[0]["channel"], "Uploader")
            self.assertEqual(videos[0]["thumbnails"][0]["url"], "https://images.test/cover.jpg")


if __name__ == "__main__":
    unittest.main()
