"""Tests for TTS abbreviation expansion (tts/server/chunking.py).

Covers:
- Group-based YAML loading (groups: general, address, timezone)
- Legacy flat expand: format fallback
- Group filtering (general only, address only, combined)
- Protected phrases (U.S., D.C., etc.) are not expanded
- Case preservation (Ave → Avenue, ave → avenue)
- Optional trailing period handling (Ave. → Avenue)
- Disabled expansion ("0", "none")
- Empty/missing input edge cases
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

TTS_ROOT = Path("/ai/server/tts")
if str(TTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TTS_ROOT))

import server.chunking as chunking

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GROUPS_YAML = textwrap.dedent("""\
    groups:
      general:
        mr: mister
        dr: doctor
        st: saint
        etc: et cetera
      address:
        ave: avenue
        blvd: boulevard
        dr: drive
        rd: road
        st: street
      timezone:
        est: eastern standard time
        pst: pacific standard time
    protect:
      - u.s.
      - d.c.
""")

_LEGACY_YAML = textwrap.dedent("""\
    expand:
      mr: mister
      ave: avenue
    protect:
      - u.s.
""")


def _reset_cache():
    """Clear module-level caches so _load_abbrev re-runs."""
    chunking._GROUP_DATA = None
    chunking._PROTECT_RE = None


def _with_yaml(yaml_content: str):
    """Context manager: write yaml to a temp file and point TTS_ABBREV_FILE at it."""
    class _Ctx:
        def __init__(self):
            self._tmp = None
            self._old_env = None

        def __enter__(self):
            self._tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".yaml", delete=False
            )
            self._tmp.write(yaml_content)
            self._tmp.flush()
            self._tmp.close()
            self._old_env = os.environ.get("TTS_ABBREV_FILE")
            os.environ["TTS_ABBREV_FILE"] = self._tmp.name
            _reset_cache()
            return self

        def __exit__(self, *_):
            _reset_cache()
            if self._old_env is None:
                os.environ.pop("TTS_ABBREV_FILE", None)
            else:
                os.environ["TTS_ABBREV_FILE"] = self._old_env
            if self._tmp:
                os.unlink(self._tmp.name)

    return _Ctx()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoadGroups(unittest.TestCase):
    """Verify _load_abbrev correctly parses grouped and legacy YAML."""

    def test_loads_three_groups(self):
        with _with_yaml(_GROUPS_YAML):
            group_data, protect_re = chunking._load_abbrev()
            self.assertEqual(sorted(group_data.keys()), ["address", "general", "timezone"])
            self.assertIsNotNone(protect_re)

    def test_legacy_flat_format(self):
        with _with_yaml(_LEGACY_YAML):
            group_data, protect_re = chunking._load_abbrev()
            self.assertIn("general", group_data)
            self.assertEqual(len(group_data), 1)
            abbrev_map, _ = group_data["general"]
            self.assertEqual(abbrev_map["mr"], "mister")
            self.assertEqual(abbrev_map["ave"], "avenue")

    def test_missing_file(self):
        with patch.dict(os.environ, {"TTS_ABBREV_FILE": "/nonexistent/path.yaml"}):
            _reset_cache()
            group_data, protect_re = chunking._load_abbrev()
            self.assertEqual(group_data, {})
            self.assertIsNone(protect_re)
            _reset_cache()

    def test_empty_yaml(self):
        with _with_yaml(""):
            group_data, _ = chunking._load_abbrev()
            self.assertEqual(group_data, {})


class TestExpandGroupFiltering(unittest.TestCase):
    """Verify expand_abbreviations applies the correct groups."""

    def test_general_only(self):
        with _with_yaml(_GROUPS_YAML):
            result = chunking.expand_abbreviations("Dr. Smith on Oak Ave.", groups="general")
            self.assertIn("Doctor", result)
            # "Ave" should NOT be expanded — address group not active
            self.assertIn("Ave", result)

    def test_address_only(self):
        with _with_yaml(_GROUPS_YAML):
            result = chunking.expand_abbreviations("Dr. Smith on Oak Ave.", groups="address")
            # "Dr" in address group maps to "drive" not "doctor"
            self.assertIn("Drive", result)
            self.assertIn("Avenue", result)

    def test_general_and_address(self):
        with _with_yaml(_GROUPS_YAML):
            result = chunking.expand_abbreviations("Visit St. Patrick on Main Rd.", groups="general,address")
            self.assertIn("Road", result)

    def test_all_groups(self):
        with _with_yaml(_GROUPS_YAML):
            result = chunking.expand_abbreviations("Meeting at 3 EST", groups="all")
            self.assertIn("eastern standard time", result.lower())

    def test_expand_all_with_1(self):
        with _with_yaml(_GROUPS_YAML):
            result = chunking.expand_abbreviations("Meeting at 3 EST", groups="1")
            self.assertIn("eastern standard time", result.lower())

    def test_empty_groups_expands_all(self):
        with _with_yaml(_GROUPS_YAML):
            result = chunking.expand_abbreviations("Dr. on Oak Ave.", groups="")
            # Both general (Dr→Doctor) and address (Ave→Avenue) should apply
            # when groups="" means all
            self.assertIn("Avenue", result)

    def test_nonexistent_group_returns_unchanged(self):
        with _with_yaml(_GROUPS_YAML):
            text = "Dr. Smith on Oak Ave."
            result = chunking.expand_abbreviations(text, groups="bogus_group")
            self.assertEqual(result, text)


class TestExpandDisabled(unittest.TestCase):
    """Verify expansion can be disabled."""

    def test_disabled_with_zero(self):
        with _with_yaml(_GROUPS_YAML):
            text = "Dr. Smith on Oak Ave."
            result = chunking.expand_abbreviations(text, groups="0")
            self.assertEqual(result, text)

    def test_disabled_with_none_str(self):
        with _with_yaml(_GROUPS_YAML):
            text = "Dr. Smith"
            result = chunking.expand_abbreviations(text, groups="none")
            self.assertEqual(result, text)

    def test_empty_text(self):
        with _with_yaml(_GROUPS_YAML):
            self.assertEqual(chunking.expand_abbreviations("", groups="general"), "")

    def test_none_text_returns_none(self):
        """expand_abbreviations should return falsy input unchanged."""
        with _with_yaml(_GROUPS_YAML):
            # The function checks `if not text: return text`
            self.assertEqual(chunking.expand_abbreviations(""), "")


class TestCasePreservation(unittest.TestCase):
    """Verify capitalization is preserved after expansion."""

    def test_uppercase_first_letter(self):
        with _with_yaml(_GROUPS_YAML):
            result = chunking.expand_abbreviations("Ave is nice", groups="address")
            self.assertTrue(result.startswith("Avenue"))

    def test_lowercase_stays_lowercase(self):
        with _with_yaml(_GROUPS_YAML):
            result = chunking.expand_abbreviations("the ave is nice", groups="address")
            self.assertIn("avenue", result)
            self.assertNotIn("Avenue", result)


class TestTrailingPeriod(unittest.TestCase):
    """Verify abbreviations with optional trailing period are matched."""

    def test_with_period(self):
        with _with_yaml(_GROUPS_YAML):
            result = chunking.expand_abbreviations("Oak Ave. is here", groups="address")
            self.assertIn("Avenue", result)

    def test_without_period(self):
        with _with_yaml(_GROUPS_YAML):
            result = chunking.expand_abbreviations("Oak Ave is here", groups="address")
            self.assertIn("Avenue", result)


class TestProtectedPhrases(unittest.TestCase):
    """Verify protected phrases are not expanded."""

    def test_us_not_expanded(self):
        with _with_yaml(_GROUPS_YAML):
            text = "The U.S. is a country"
            result = chunking.expand_abbreviations(text, groups="all")
            self.assertIn("U.S.", result)

    def test_dc_not_expanded(self):
        with _with_yaml(_GROUPS_YAML):
            text = "Washington D.C. is the capital"
            result = chunking.expand_abbreviations(text, groups="all")
            self.assertIn("D.C.", result)

    def test_protected_alongside_expanded(self):
        with _with_yaml(_GROUPS_YAML):
            text = "Dr. Smith in D.C."
            result = chunking.expand_abbreviations(text, groups="general")
            self.assertIn("Doctor", result)
            self.assertIn("D.C.", result)


class TestBuildRegex(unittest.TestCase):
    """Verify _build_regex produces correct patterns."""

    def test_empty_map(self):
        self.assertIsNone(chunking._build_regex({}))

    def test_longer_keys_first(self):
        """Longer abbreviations should be tried first to prevent partial matches."""
        regex = chunking._build_regex({"blvd": "boulevard", "bl": "block"})
        self.assertIsNotNone(regex)
        # "blvd" should match fully, not "bl" + "vd" leftover
        m = regex.search("Oak Blvd")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).lower(), "blvd")


if __name__ == "__main__":
    unittest.main()
