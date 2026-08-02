"""Small shared domain types with no provider, storage, or CLI dependency."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from zoneinfo import ZoneInfo

EventSink = Callable[[str, Mapping[str, object]], None]
NEW_YORK = ZoneInfo("America/New_York")
