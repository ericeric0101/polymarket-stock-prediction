"""Backward-compatible facade for shadow journal storage."""

from __future__ import annotations

from .storage.journal_core import JournalCoreRepository
from .storage.journal_models import (
    BufferSweepObservation,
    CheckpointObservation,
    ExecutionObservation,
    FirstSignalCalibrationObservation,
    MakerShadowQuote,
    PaperBatchEntry,
    PaperBatchResult,
    PaperPosition,
    ReplayObservation,
    SpotSourceComparison,
    StoredMarketCandidate,
    StoredOutcomeToken,
    StoredSpotObservation,
)
from .storage.journal_observations import JournalObservationRepository
from .storage.journal_paper import JournalPaperRepository
from .storage.journal_research import JournalResearchRepository
from .storage.schema import CORE_SCHEMA

SCHEMA = CORE_SCHEMA


class ShadowJournal(
    JournalCoreRepository,
    JournalObservationRepository,
    JournalResearchRepository,
    JournalPaperRepository,
):
    """Compatibility facade; SQLite implementation lives in :mod:`storage`."""


__all__ = (
    "BufferSweepObservation",
    "CheckpointObservation",
    "ExecutionObservation",
    "FirstSignalCalibrationObservation",
    "MakerShadowQuote",
    "PaperBatchEntry",
    "PaperBatchResult",
    "PaperPosition",
    "ReplayObservation",
    "SCHEMA",
    "ShadowJournal",
    "SpotSourceComparison",
    "StoredMarketCandidate",
    "StoredOutcomeToken",
    "StoredSpotObservation",
)
