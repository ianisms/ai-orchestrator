"""Tests for TTS_EXPAND settings validator and _expand_context logic.

Covers:
- Settings.TTS_EXPAND field_validator coercion (int → str)
- UtteranceRunner._expand_context tool-aware group selection
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

MODULES_ROOT = Path("/ai/server/modules")
SERVER_ROOT = Path("/ai/server")
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
if str(MODULES_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULES_ROOT))

IMPORT_ERROR = None
try:
    from orchestrator.config.settings import Settings
except Exception as exc:
    IMPORT_ERROR = exc


@unittest.skipIf(IMPORT_ERROR is not None, f"missing deps: {IMPORT_ERROR!r}")
class TestTTSExpandValidator(unittest.TestCase):
    """Verify the field_validator on TTS_EXPAND handles int and str inputs."""

    def test_int_zero_becomes_none(self):
        s = Settings(TTS_EXPAND=0)
        self.assertEqual(s.TTS_EXPAND, "none")

    def test_int_one_becomes_general(self):
        s = Settings(TTS_EXPAND=1)
        self.assertEqual(s.TTS_EXPAND, "general")

    def test_int_truthy_becomes_general(self):
        s = Settings(TTS_EXPAND=2)
        self.assertEqual(s.TTS_EXPAND, "general")

    def test_string_passthrough(self):
        s = Settings(TTS_EXPAND="general,address")
        self.assertEqual(s.TTS_EXPAND, "general,address")

    def test_string_general(self):
        s = Settings(TTS_EXPAND="general")
        self.assertEqual(s.TTS_EXPAND, "general")

    def test_string_none(self):
        s = Settings(TTS_EXPAND="none")
        self.assertEqual(s.TTS_EXPAND, "none")

    def test_default_value(self):
        s = Settings()
        self.assertEqual(s.TTS_EXPAND, "general")


@unittest.skipIf(IMPORT_ERROR is not None, f"missing deps: {IMPORT_ERROR!r}")
class TestExpandContext(unittest.TestCase):
    """Verify UtteranceRunner._expand_context selects groups based on tools used."""

    def _make_runner(self):
        """Create a minimal UtteranceRunner with mocked dependencies."""
        from orchestrator.core.utterance import UtteranceRunner

        runner = UtteranceRunner.__new__(UtteranceRunner)
        runner._last_used_tools = set()
        return runner

    def test_no_tools_returns_default(self):
        runner = self._make_runner()
        with patch("orchestrator.core.utterance.settings") as mock_settings:
            mock_settings.TTS_EXPAND = "general"
            result = runner._expand_context()
            self.assertEqual(result, "general")

    def test_places_tool_adds_address(self):
        runner = self._make_runner()
        runner._last_used_tools = {"local_places_search"}
        with patch("orchestrator.core.utterance.settings") as mock_settings:
            mock_settings.TTS_EXPAND = "general"
            result = runner._expand_context()
            self.assertEqual(result, "general,address")

    def test_place_details_tool_adds_address(self):
        runner = self._make_runner()
        runner._last_used_tools = {"place_details"}
        with patch("orchestrator.core.utterance.settings") as mock_settings:
            mock_settings.TTS_EXPAND = "general"
            result = runner._expand_context()
            self.assertEqual(result, "general,address")

    def test_place_summary_tool_adds_address(self):
        runner = self._make_runner()
        runner._last_used_tools = {"place_summary"}
        with patch("orchestrator.core.utterance.settings") as mock_settings:
            mock_settings.TTS_EXPAND = "general"
            result = runner._expand_context()
            self.assertEqual(result, "general,address")

    def test_non_places_tool_no_address(self):
        runner = self._make_runner()
        runner._last_used_tools = {"weather_forecast", "stock_quote"}
        with patch("orchestrator.core.utterance.settings") as mock_settings:
            mock_settings.TTS_EXPAND = "general"
            result = runner._expand_context()
            self.assertEqual(result, "general")

    def test_address_already_in_base_not_duplicated(self):
        runner = self._make_runner()
        runner._last_used_tools = {"local_places_search"}
        with patch("orchestrator.core.utterance.settings") as mock_settings:
            mock_settings.TTS_EXPAND = "general,address"
            result = runner._expand_context()
            self.assertEqual(result, "general,address")

    def test_empty_base_with_places_tool(self):
        runner = self._make_runner()
        runner._last_used_tools = {"local_places_search"}
        with patch("orchestrator.core.utterance.settings") as mock_settings:
            mock_settings.TTS_EXPAND = ""
            result = runner._expand_context()
            # Empty TTS_EXPAND falls back to "general" via `or "general"`
            self.assertEqual(result, "general,address")

    def test_disabled_base_with_places_tool(self):
        runner = self._make_runner()
        runner._last_used_tools = {"local_places_search"}
        with patch("orchestrator.core.utterance.settings") as mock_settings:
            mock_settings.TTS_EXPAND = "none"
            result = runner._expand_context()
            # "none" doesn't contain "address", so address is appended
            self.assertEqual(result, "none,address")


if __name__ == "__main__":
    unittest.main()
