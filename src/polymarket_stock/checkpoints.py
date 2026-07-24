"""Fixed New York decision checkpoints for non-leaking model research."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
CHECKPOINTS = ((10, 0, "1000_EDT"), (12, 0, "1200_EDT"), (14, 0, "1400_EDT"), (15, 30, "1530_EDT"))


def latest_checkpoint(now: datetime) -> tuple[str, str] | None:
    """Return the latest same-day regular-session checkpoint, if one has passed."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(NEW_YORK)
    selected = None
    for hour, minute, name in CHECKPOINTS:
        if (local.hour, local.minute) >= (hour, minute):
            selected = name
    return (local.date().isoformat(), selected) if selected else None
