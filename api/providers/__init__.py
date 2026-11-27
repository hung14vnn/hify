"""Provider adapters (Spotify catalog, YouTube Music, Eclipse FLAC bridge)."""

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any


@dataclass
class PlaybackRecording:
    """Source of bytes delivered on one media request, including URL refreshes."""
    key: str | None = ""

    def observe(self, key: str | None) -> None:
        self.key = key if self.key in ("", key) else None


def start_lookup[K, V](
    pending: dict[K, asyncio.Task[V]], key: K, lookup: Coroutine[Any, Any, V]
) -> asyncio.Task[V]:
    task = asyncio.create_task(lookup)
    pending[key] = task

    def finished(done: asyncio.Task[V]) -> None:
        del pending[key]
        if not done.cancelled():
            done.exception()  # Also observe failures when every caller disconnected.

    task.add_done_callback(finished)
    return task
