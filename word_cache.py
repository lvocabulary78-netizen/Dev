"""
word_cache.py — Loads words.json once at startup and keeps everything in RAM.
No database queries for word lookups during a game.

Structure after loading:
    _cache = {
        "A1": {
            "Daily Actions": [ {word, synonyms, arabic, examples, level, category,
                                pronunciation, fact, collocations, emoji, proverb}, ... ],
            "Emotions":      [ ... ],
            ...
        },
        "A2": { ... },
        "B1": { ... },
    }

Each entry is normalized to always contain an "examples" field (list of strings).
New optional fields (pronunciation, fact, collocations, emoji, proverb) default to
None / [] when absent so downstream code can safely call .get() on them.

Note: examples in the new JSON are bilingual strings with a newline separating the
English sentence from the Arabic translation — they are kept as-is.
"""

import json
import logging
from config import WORDS_FILE

logger = logging.getLogger(__name__)

# Public cache — do not mutate after load_words() is called
_cache: dict[str, dict[str, list[dict]]] = {}
_total: int = 0


def _normalize_entry(entry: dict) -> dict:
    """
    Normalize a word entry so all downstream code can rely on consistent fields.

    Guaranteed after normalization:
      - examples      : list[str]  — may contain bilingual "EN\nAR" strings (new format)
      - synonyms      : list[str]
      - collocations  : list[str]  — empty list if absent (old format)
      - pronunciation : str | None
      - fact          : str | None
      - emoji         : str | None
      - proverb       : str | None
    """
    normalized = entry.copy()

    # ── examples / example ────────────────────────────────────────────────
    if "examples" in normalized:
        if not isinstance(normalized["examples"], list):
            logger.warning(
                "Entry for '%s' has 'examples' that is not a list; wrapping.",
                entry.get("word"),
            )
            normalized["examples"] = [str(normalized["examples"])]
    elif "example" in normalized:
        example_val = normalized.pop("example")
        normalized["examples"] = (
            [example_val] if isinstance(example_val, str) else [str(example_val)]
        )
        logger.debug(
            "Converted 'example' to 'examples' list for word '%s'", entry.get("word")
        )
    else:
        logger.warning(
            "Entry for '%s' lacks both 'examples' and 'example' fields.", entry.get("word")
        )
        normalized["examples"] = []

    # ── synonyms (defensive) ──────────────────────────────────────────────
    if "synonyms" in normalized and not isinstance(normalized["synonyms"], list):
        normalized["synonyms"] = [str(normalized["synonyms"])]

    # ── new optional fields — default so .get() is never needed downstream ─
    normalized.setdefault("collocations", [])
    normalized.setdefault("pronunciation", None)
    normalized.setdefault("fact", None)
    normalized.setdefault("emoji", None)
    normalized.setdefault("proverb", None)

    # Ensure collocations is a list (defensive)
    if not isinstance(normalized["collocations"], list):
        normalized["collocations"] = [str(normalized["collocations"])]

    return normalized


def load_words(filepath: str = WORDS_FILE) -> None:
    """
    Parse the JSON vocabulary file, normalize entries, and index by level → category.
    Call once at startup.
    """
    global _cache, _total

    with open(filepath, "r", encoding="utf-8") as fh:
        words: list[dict] = json.load(fh)

    _cache.clear()
    normalized_count = 0

    for entry in words:
        normalized = _normalize_entry(entry)
        level = normalized.get("level", "A1")
        category = normalized.get("category", "General")
        _cache.setdefault(level, {}).setdefault(category, []).append(normalized)
        normalized_count += 1

    _total = normalized_count
    level_summary = {
        lvl: sum(len(v) for v in cats.values())
        for lvl, cats in _cache.items()
    }
    logger.info(
        "Loaded %d words (normalized) | Levels: %s",
        _total,
        level_summary
    )


def get_levels() -> list[str]:
    """Return all levels present in the dataset."""
    return sorted(_cache.keys())


def get_categories(level: str) -> list[str]:
    """Return all categories available for a given level."""
    return sorted(_cache.get(level, {}).keys())


def get_words(level: str, category: str) -> list[dict]:
    """Return all word entries for a level + category combination."""
    return _cache.get(level, {}).get(category, [])


def word_count(level: str | None = None, category: str | None = None) -> int:
    """Convenience: count words matching optional filters."""
    if level and category:
        return len(get_words(level, category))
    if level:
        return sum(len(v) for v in _cache.get(level, {}).values())
    return _total