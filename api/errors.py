from __future__ import annotations


class SubsonicError(Exception):
    """A Subsonic failure rendered as an error envelope with HTTP 200."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
