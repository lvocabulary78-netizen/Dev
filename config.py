"""
config.py — Central configuration for the Synonym Game Bot
(Simple side-project version — no environment dependency)
"""

import logging

_logger = logging.getLogger(__name__)

# ── Bot credentials ────────────────────────────────────────────────────────
BOT_TOKEN: str = "8732231617:AAFoiNC-Vg0M-9Kai23yMnlP7xuRGvAXf0M"

if not BOT_TOKEN:
    _logger.critical("BOT_TOKEN is missing!")
    raise RuntimeError("BOT_TOKEN is required")

# ── Admins ─────────────────────────────────────────────────────────────────
SUPER_ADMIN_IDS = [
    7161553913,
    6526832001,
    6360539372,
    1093029825
]

# ── File paths ─────────────────────────────────────────────────────────────
DB_PATH = "synonym_game.db"
WORDS_FILE = "words.json"

# ── Game settings ──────────────────────────────────────────────────────────
DEFAULT_NUM_QUESTIONS = 10
DEFAULT_TIME_PER_ROUND = 90  # seconds

# ── Scoring ────────────────────────────────────────────────────────────────
CORRECT_POINTS = 5
HINT_COST = 2
SKIP_COST = 1

# ── Supported levels ───────────────────────────────────────────────────────
LEVELS = ["A1", "A2", "B1"]

# ── Fuzzy matching ─────────────────────────────────────────────────────────
FUZZY_THRESHOLD = 0.82

# ── Access control ─────────────────────────────────────────────────────────
ALLOW_PRIVATE_GAMES = True