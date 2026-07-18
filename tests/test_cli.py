from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from polymarket_stock.cli import _report_public_api_failure
from polymarket_stock.config import Settings
from polymarket_stock.http import PublicApiError


class CliTests(unittest.TestCase):
    def test_tls_error_is_reported_without_disabling_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                shadow_mode=True,
                live_trading_enabled=False,
                journal_path=Path(directory) / "journal.db",
                log_path=Path(directory) / "events.jsonl",
            )
            with self.assertRaisesRegex(SystemExit, "TLS verification failed"):
                _report_public_api_failure(
                    settings,
                    "MARKET_SCAN_FAILED",
                    PublicApiError("GET example failed: CERTIFICATE_VERIFY_FAILED"),
                )
            self.assertIn("MARKET_SCAN_FAILED", settings.log_path.read_text(encoding="utf-8"))
