"""Base contract shared by journal storage repositories."""

from __future__ import annotations

from pathlib import Path


class JournalRepository:
    path: Path
