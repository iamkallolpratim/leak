"""Progress reporting for long-running ingestion.

Ingesters call these three methods and know nothing about terminals; the
CLI plugs in a rich-based animation, tests plug in a recorder, and library
callers get the no-op default.
"""

from __future__ import annotations

from typing import Protocol


class Progress(Protocol):
    def start(self, key: str, label: str, total: int | None) -> None:
        """Begin a task. `total=None` means unknown size (spinner only)."""

    def advance(self, key: str, n: int = 1, detail: str = "") -> None: ...

    def finish(self, key: str, summary: str = "") -> None: ...


class NullProgress:
    def start(self, key: str, label: str, total: int | None) -> None:
        pass

    def advance(self, key: str, n: int = 1, detail: str = "") -> None:
        pass

    def finish(self, key: str, summary: str = "") -> None:
        pass
