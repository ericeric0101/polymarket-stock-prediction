"""Fixed New York decision checkpoints for non-leaking model research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
CHECKPOINTS = ((10, 0, "1000_EDT"), (12, 0, "1200_EDT"), (14, 0, "1400_EDT"), (15, 30, "1530_EDT"))
DEFAULT_MAXIMUM_DELAY_SECONDS = 300.0


@dataclass(frozen=True)
class CheckpointWindow:
    checkpoint_date: str
    checkpoint_name: str
    target_at: datetime
    delay_seconds: float


def checkpoint_target_at(checkpoint_date: str, checkpoint_name: str) -> datetime:
    """Return the exact New York checkpoint timestamp normalized to UTC."""

    try:
        date_value = datetime.fromisoformat(checkpoint_date).date()
        hour, minute, _ = next(item for item in CHECKPOINTS if item[2] == checkpoint_name)
    except (StopIteration, ValueError) as error:
        raise ValueError("unknown checkpoint") from error
    return datetime(date_value.year, date_value.month, date_value.day, hour, minute, tzinfo=NEW_YORK).astimezone(UTC)


def latest_checkpoint(now: datetime, maximum_delay_seconds: float | None = None) -> tuple[str, str] | None:
    """Return the latest same-day regular-session checkpoint, if one has passed."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if maximum_delay_seconds is not None and maximum_delay_seconds < 0:
        raise ValueError("maximum_delay_seconds must be non-negative")
    local = now.astimezone(NEW_YORK)
    selected = None
    for hour, minute, name in CHECKPOINTS:
        if (local.hour, local.minute) >= (hour, minute):
            selected = name
    if selected is None:
        return None
    result = (local.date().isoformat(), selected)
    if maximum_delay_seconds is not None:
        target_at = checkpoint_target_at(*result)
        if (now - target_at).total_seconds() > maximum_delay_seconds:
            return None
    return result


def checkpoint_window(now: datetime, maximum_delay_seconds: float = DEFAULT_MAXIMUM_DELAY_SECONDS) -> CheckpointWindow | None:
    """Return the current valid capture window, never backfilling a late start."""

    checkpoint = latest_checkpoint(now, maximum_delay_seconds=maximum_delay_seconds)
    if checkpoint is None:
        return None
    target_at = checkpoint_target_at(*checkpoint)
    return CheckpointWindow(*checkpoint, target_at=target_at, delay_seconds=max(0.0, (now - target_at).total_seconds()))
