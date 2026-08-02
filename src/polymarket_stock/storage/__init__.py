"""Shared storage infrastructure."""

from .sqlite import database_connection
from .writer import JournalWriter

__all__ = ("JournalWriter", "database_connection")
