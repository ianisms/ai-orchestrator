import logging
import os
import re
from pathlib import Path
from typing import List, Tuple, Optional, Dict

_LOG = logging.getLogger("tts.chunking")

# Cached per-group data: {group_name: (map, compiled_regex)}
_GROUP_DATA: dict[str, tuple[dict[str, str], re.Pattern]] | None = None
_PROTECT_RE: re.Pattern | None = None


def _build_regex(abbrev_map: dict[str, str]) -> re.Pattern | None:
    if not abbrev_map:
        return None
    sorted_keys = sorted(abbrev_map.keys(), key=len, reverse=True)
    escaped_keys = [re.escape(k) for k in sorted_keys]
    pattern = r"\b(" + "|".join(escaped_keys) + r")\.?\b"
    return re.compile(pattern, re.IGNORECASE)


def _load_abbrev() -> tuple[dict[str, tuple[dict[str, str], re.Pattern]], re.Pattern | None]:
    path = Path(os.getenv("TTS_ABBREV_FILE", str(Path(__file__).resolve().parent.parent / "config" / "abbrev.yaml")))
    if not path.exists():
        _LOG.warning("abbrev file not found: %s", path)
        return {}, None
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        _LOG.warning("failed to load abbrev file %s: %s", path, exc)
        return {}, None

    group_data: dict[str, tuple[dict[str, str], re.Pattern]] = {}
    groups = data.get("groups")
    if isinstance(groups, dict):
        for group_name, entries in groups.items():
            if not isinstance(entries, dict):
                continue
            abbrev_map: dict[str, str] = {}
            for abbr, full in entries.items():
                key = str(abbr).strip().lower()
                val = str(full).strip()
                if key and val:
                    abbrev_map[key] = val
            regex = _build_regex(abbrev_map)
            if abbrev_map and regex:
                group_data[str(group_name).strip().lower()] = (abbrev_map, regex)
    else:
        # Legacy flat "expand:" format — load as "general" group
        raw = data.get("expand")
        if isinstance(raw, dict):
            abbrev_map = {}
            for abbr, full in raw.items():
                key = str(abbr).strip().lower()
                val = str(full).strip()
                if key and val:
                    abbrev_map[key] = val
            regex = _build_regex(abbrev_map)
            if abbrev_map and regex:
                group_data["general"] = (abbrev_map, regex)

    protect_re = None
    protect = data.get("protect")
    if isinstance(protect, list) and protect:
        escaped = [re.escape(str(p).strip()) for p in protect if str(p).strip()]
        if escaped:
            protect_re = re.compile("|".join(escaped), re.IGNORECASE)

    available = sorted(group_data.keys())
    _LOG.info("loaded abbreviation groups: %s", ", ".join(available) if available else "(none)")
    return group_data, protect_re


def _get_abbrev() -> tuple[dict[str, tuple[dict[str, str], re.Pattern]], re.Pattern | None]:
    global _GROUP_DATA, _PROTECT_RE
    if _GROUP_DATA is None:
        _GROUP_DATA, _PROTECT_RE = _load_abbrev()
    return _GROUP_DATA, _PROTECT_RE


def expand_abbreviations(text: str, groups: str = "") -> str:
    """Expand abbreviations in text.

    groups: comma-separated group names (e.g. "general", "general,address").
            Empty string or "all" expands all groups.
            "0" or "none" disables expansion.
    """
    if not text:
        return text
    group_data, protect_re = _get_abbrev()
    if not group_data:
        return text

    groups_str = str(groups or "").strip().lower()
    if groups_str in ("0", "none"):
        return text

    # Determine which groups to apply
    if not groups_str or groups_str in ("all", "1"):
        active_groups = list(group_data.keys())
    else:
        requested = {g.strip() for g in groups_str.split(",") if g.strip()}
        active_groups = [g for g in requested if g in group_data]
    if not active_groups:
        return text

    # Mark protected phrases with placeholders
    protected: list[str] = []
    if protect_re:
        def _protect_sub(m: re.Match) -> str:
            protected.append(m.group(0))
            return f"\x00PROT{len(protected) - 1}\x00"
        text = protect_re.sub(_protect_sub, text)

    # Apply each active group
    for group_name in active_groups:
        abbrev_map, abbrev_re = group_data[group_name]

        def _replace(m: re.Match, _map: dict[str, str] = abbrev_map) -> str:
            matched = m.group(1).lower()
            replacement = _map.get(matched, matched)
            original = m.group(1)
            if original[0].isupper():
                return replacement.capitalize()
            return replacement

        text = abbrev_re.sub(_replace, text)

    # Restore protected phrases
    for i, orig in enumerate(protected):
        text = text.replace(f"\x00PROT{i}\x00", orig)

    return text


def _int_or_default(val, default: int) -> int:
    return default if val is None else int(val)


def smart_chunks(
    text: str,
    max_chars: int = 900,
    opts: Optional[Dict] = None,
) -> List[Tuple[str, int]]:
    opts = opts or {}

    env_min_chars = int(os.getenv("TTS_MIN_CHUNK_CHARS", "35"))
    env_min_words = int(os.getenv("TTS_MIN_CHUNK_WORDS", "5"))

    min_chars = _int_or_default(opts.get("min_chunk_chars"), env_min_chars)
    min_words = _int_or_default(opts.get("min_chunk_words"), env_min_words)

    # Sentence split
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    parts = [p.strip() for p in parts if p.strip()]

    chunks: List[str] = []

    for p in parts:
        if len(p) <= max_chars:
            chunks.append(p)
        else:
            words = p.split()
            buf = ""
            for w in words:
                if not buf or len(buf) + len(w) + 1 <= max_chars:
                    buf = f"{buf} {w}".strip()
                else:
                    chunks.append(buf)
                    buf = w
            if buf:
                chunks.append(buf)

    # Merge tiny chunks
    merged: List[Tuple[str, int]] = []
    for c in chunks:
        if merged and (len(c) < min_chars or len(c.split()) < min_words):
            merged[-1] = (merged[-1][0].rstrip() + " " + c.lstrip(), 0)
        else:
            merged.append((c, 0))

    return merged
