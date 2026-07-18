"""Runtime configuration with an intentional Phase 0 no-trading boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


class ConfigurationError(ValueError):
    """Raised when configuration would weaken the Phase 0 safety boundary."""


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Load simple KEY=VALUE pairs without overriding the invoking environment."""

    if not path.is_file():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"invalid .env entry at line {line_number}")
        name, value = line.split("=", 1)
        name = name.strip()
        if not name:
            raise ConfigurationError(f"missing .env variable name at line {line_number}")
        os.environ.setdefault(name, value.strip().strip('"').strip("'"))


def _required_bool(name: str, expected: bool) -> bool:
    raw_value = os.getenv(name, str(expected)).strip().lower()
    if raw_value not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be true or false")

    actual = raw_value == "true"
    if actual is not expected:
        expected_text = str(expected).lower()
        raise ConfigurationError(
            f"Phase 0 requires {name}={expected_text}; refusing to start"
        )
    return actual


@dataclass(frozen=True)
class Settings:
    """Only local journal and log locations are configurable in Phase 0."""

    shadow_mode: bool
    live_trading_enabled: bool
    journal_path: Path
    log_path: Path

    @classmethod
    def from_environment(cls) -> "Settings":
        _load_dotenv()
        return cls(
            shadow_mode=_required_bool("SHADOW_MODE", expected=True),
            live_trading_enabled=_required_bool("LIVE_TRADING_ENABLED", expected=False),
            journal_path=Path(os.getenv("JOURNAL_PATH", "data/shadow_journal.db")),
            log_path=Path(os.getenv("LOG_PATH", "logs/shadow_bot.jsonl")),
        )
