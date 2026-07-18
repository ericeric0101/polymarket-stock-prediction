from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from polymarket_stock.config import ConfigurationError, Settings, _load_dotenv


class SettingsTests(unittest.TestCase):
    def test_default_settings_are_shadow_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_environment()
        self.assertTrue(settings.shadow_mode)
        self.assertFalse(settings.live_trading_enabled)

    def test_live_trading_is_refused(self) -> None:
        with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()

    def test_dotenv_does_not_override_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = Path(directory) / ".env"
            dotenv_path.write_text("SHADOW_MODE=false\n", encoding="utf-8")
            with patch.dict(os.environ, {"SHADOW_MODE": "true"}, clear=True):
                _load_dotenv(dotenv_path)
                self.assertEqual(os.environ["SHADOW_MODE"], "true")
