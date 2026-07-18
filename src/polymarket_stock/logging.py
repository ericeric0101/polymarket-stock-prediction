"""Small JSONL logger to keep shadow runs machine-readable and deterministic."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Mapping


def log_event(path: Path, event_type: str, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": event_type,
        "recorded_at": datetime.now(UTC).isoformat(),
        "payload": dict(payload),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str))
        handle.write("\n")
