"""Measure quiet intros and outros from audio already delivered by the playback proxy."""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from collections import OrderedDict
from pathlib import Path

from fastapi.responses import StreamingResponse

import db
from providers import PlaybackRecording

logger = logging.getLogger(__name__)
VERSION = 1
_MAX_BYTES = 256 * 1024 * 1024
_MAX_JOBS = 2
_TOKEN_TTL = 2 * 60 * 60
_MAX_TOKENS = 4096
_CHUNK_BYTES = 128 * 1024
_RATE = 8000
_FRAME_SECONDS = 0.25
_EDGE_SECONDS = 24
# Only self-contained audio containers. Never open a playlist, network URL or
# external track reference supplied by downloaded content.
_INPUT_OPTIONS = ["-protocol_whitelist", "file", "-format_whitelist", "flac,mp3,mov,matroska,webm,ogg,aac,wav"]


def _edge_seconds(pcm: bytes, reverse: bool) -> float:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if reverse:
        samples.reverse()
    frame = int(_RATE * _FRAME_SECONDS)
    powers = []
    for start in range(0, len(samples) - frame + 1, frame):
        powers.append(sum(x * x for x in samples[start:start + frame]) / frame)
    if len(powers) < 8:
        return 0.0
    # Compare the edge to its own louder frames. A run of sustained energy
    # marks the end of a quiet section; a single drum hit doesn't.
    reference = sorted(powers)[int(len(powers) * 0.85)]
    if reference < 100 ** 2:
        return 0.0
    threshold = reference * 0.32  # about 5 dB below the established level
    for i in range(len(powers) - 3):
        if powers[i] >= threshold and sum(p >= threshold for p in powers[i:i + 4]) >= 3:
            return i * _FRAME_SECONDS if i >= 4 else 0.0
    return 0.0


def _measure(path: Path) -> tuple[float, float] | None:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", *_INPUT_OPTIONS, "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True,
    )
    try:
        duration = float(probe.stdout)
    except ValueError:
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    if duration < 8:
        return 0.0, 0.0
    window = min(_EDGE_SECONDS, duration / 2)
    cues = []
    for start, reverse in ((0.0, False), (duration - window, True)):
        decoded = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", *_INPUT_OPTIONS,
             "-threads", "1", "-ss", str(start), "-i", str(path), "-map", "0:a:0",
             "-t", str(window), "-ac", "1", "-ar", str(_RATE), "-f", "s16le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12, check=True,
        ).stdout
        if len(decoded) < (window - 0.5) * _RATE * 2:
            return None
        cues.append(_edge_seconds(decoded, reverse))
    return cues[0], cues[1]


class _Capture:
    def __init__(self) -> None:
        # At most 8 MiB queued per active song. The proxy drops the analysis
        # if storage falls behind; audio delivery never waits for space.
        self.chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self.stopped = threading.Event()

    def cancel(self) -> None:
        self.stopped.set()
        try:
            self.chunks.put_nowait(None)
        except queue.Full:
            pass

    def offer(self, data: bytes | None) -> None:
        if self.stopped.is_set():
            return
        try:
            if data is None:
                self.chunks.put_nowait(None)
            else:
                for start in range(0, len(data), _CHUNK_BYTES):
                    if self.stopped.is_set():
                        return
                    self.chunks.put_nowait(data[start:start + _CHUNK_BYTES])
        except queue.Full:
            self.cancel()

    def process(self, recording_id: str) -> None:
        try:
            if self.stopped.is_set() or db.auto_mix_info(recording_id, VERSION) is not None:
                return
            with tempfile.TemporaryDirectory(prefix="hify-mix-") as folder:
                path = Path(folder) / "audio"
                with path.open("wb") as output:
                    deadline = time.monotonic() + 15 * 60
                    while not self.stopped.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return
                        chunk = self.chunks.get(timeout=min(45, remaining))
                        if chunk is None:
                            break
                        output.write(chunk)
                if self.stopped.is_set():
                    return
                result = _measure(path)
                if result is not None and not self.stopped.is_set():
                    db.save_auto_mix_info(recording_id, VERSION, *result)
        except queue.Empty:
            pass  # Client stopped consuming the stream.
        except (subprocess.SubprocessError, OSError, ValueError):
            logger.debug("AutoMix audio unavailable for analysis")
        finally:
            self.stopped.set()
            # The HTTP stream can outlive a cache hit or a failed capture. Free
            # queued chunks now even if a slow client stays connected.
            while True:
                try:
                    self.chunks.get_nowait()
                except queue.Empty:
                    break


class AutoMix:
    def __init__(self) -> None:
        self._active: dict[str, tuple[_Capture, asyncio.Task]] = {}
        self._bindings: OrderedDict[tuple[str, str, str], tuple[PlaybackRecording, float]] = OrderedDict()
        self._closed = False

    def _start(self, recording_id: str) -> _Capture | None:
        if self._closed or recording_id in self._active or len(self._active) >= _MAX_JOBS:
            return None
        capture = _Capture()

        async def run() -> None:
            try:
                await asyncio.to_thread(capture.process, recording_id)
            except Exception:
                logger.exception("AutoMix background analysis failed")
            finally:
                self._active.pop(recording_id, None)

        self._active[recording_id] = capture, asyncio.create_task(run())
        return capture

    def _bind(self, key: tuple[str, str, str], recording: PlaybackRecording) -> None:
        now = time.monotonic()
        prior = self._bindings.pop(key, None)
        if prior and prior[1] > now and prior[0].key != recording.key:
            prior[0].key = recording.key = None  # Conflicting uses of one token stay ambiguous.
        self._bindings[key] = (recording, now + _TOKEN_TTL)
        if len(self._bindings) > _MAX_TOKENS:
            self._bindings.popitem(last=False)

    async def info(self, user_id: str, song_id: str, token: str) -> dict[str, float] | None:
        binding = self._bindings.get((user_id, song_id, token))
        if not binding or binding[1] <= time.monotonic() or not binding[0].key:
            return None
        key = binding[0].key
        info = await asyncio.to_thread(db.auto_mix_info, key, VERSION)
        # An overlapping range may have switched recordings during the SQLite read.
        current = self._bindings.get((user_id, song_id, token))
        return info if current and current[0].key == key and current[1] > time.monotonic() else None

    def observe(self, user_id: str, song_id: str, token: str, recording: PlaybackRecording,
                response: StreamingResponse, requested_range: str | None) -> None:
        requested_range = (requested_range or "").lower().strip()
        if response.status_code not in (200, 206) or requested_range == "bytes=0-1":
            return
        expected = response.headers.get("content-length")
        size = int(expected) if expected and expected.isdecimal() and len(expected) <= 18 else None
        full = response.status_code == 200 and requested_range in ("", "bytes=0-")
        content_range = response.headers.get("content-range")
        if response.status_code == 206 or content_range:
            match = re.fullmatch(r"bytes 0-(\d{1,18})/(\d{1,18})", content_range or "", re.I)
            full = bool(match and int(match[1]) + 1 == int(match[2]) and size in (None, int(match[2])))
            if full:
                size = int(match[2])
        can_capture = full and (size is None or 0 < size <= _MAX_BYTES)

        source = response.body_iterator
        binding_key = (user_id, song_id, token)

        async def body():
            capture = None
            first = True
            recording_id = None
            total = 0
            complete = False
            try:
                async for chunk in source:
                    if first and chunk and not self._closed:
                        self._bind(binding_key, recording)
                        recording_id = recording.key
                    elif recording_id and recording.key != recording_id:
                        # A refreshed upstream audio range switched recordings.
                        # Invalidate any parallel request for the same media load.
                        current = self._bindings.get(binding_key)
                        if current:
                            current[0].key = None
                        if capture:
                            capture.cancel()
                        recording_id = None
                    yield chunk
                    # Only start background work once the first bytes have gone out.
                    if first and chunk:
                        first = False
                        if recording_id and can_capture:
                            capture = self._start(recording_id)
                    if capture and not capture.stopped.is_set():
                        total += len(chunk)
                        if total > _MAX_BYTES:
                            capture.cancel()
                        else:
                            capture.offer(chunk)
                complete = recording.key == recording_id and total > 0 and (size is None or total == size)
            finally:
                if capture:
                    if complete:
                        capture.offer(None)
                    else:
                        capture.cancel()
                await source.aclose()

        response.body_iterator = body()

    async def close(self) -> None:
        self._closed = True
        self._bindings.clear()
        jobs = list(self._active.values())
        for capture, _ in jobs:
            capture.cancel()
        await asyncio.gather(*(task for _, task in jobs))
