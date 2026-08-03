"""Dependencies shared by command handlers."""

from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass

from ..config import Settings
from ..journal import ShadowJournal


@dataclass(frozen=True)
class CommandContext:
    """Initialized configuration and journal for one CLI invocation."""

    arguments: Namespace
    settings: Settings
    journal: ShadowJournal
