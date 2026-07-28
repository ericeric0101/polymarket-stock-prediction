from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from polymarket_stock.cli import _await_with_graceful_shutdown, _report_public_api_failure
from polymarket_stock.config import Settings
from polymarket_stock.http import PublicApiError


class GracefulShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_wrapper_waits_for_child_finalizer(self) -> None:
        started = asyncio.Event()
        finalized = asyncio.Event()

        async def worker() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                finalized.set()

        wrapper = asyncio.create_task(_await_with_graceful_shutdown(worker()))
        await started.wait()
        wrapper.cancel()
        self.assertTrue(await wrapper)
        self.assertTrue(finalized.is_set())


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
