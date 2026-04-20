"""
config.py — Central configuration for the Synonym Game Bot.

IMPORTANT:  Never hard-code BOT_TOKEN here.
            Set it as an environment variable before running:
                export BOT_TOKEN="your_token"
            On Render: add it in the service's Environment panel.
"""

import os
import logging

_logger = logging.getLogger(__name__)

# ── Bot credentials ────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "8732231617:AAFoiNC-Vg0M-9Kai23yMnlP7xuRGvAXf0M")
if not BOT_TOKEN:
    _logger.critical(
        "BOT_TOKEN is not set!  "
        "Export it as an environment variable before running the bot."
    )
    raise EnvironmentError("BOT_TOKEN environment variable is required.")

# Telegram user IDs of global super-admins.
# Super-admins can use every admin command in every group, even without being
# a Telegram group admin.
SUPER_ADMIN_IDS: list[int] = [7161553913, 6526832001, 6360539372, 1093029825]

# ── File paths ─────────────────────────────────────────────────────────────
DB_PATH    = os.environ.get("DB_PATH",    "synonym_game.db")
WORDS_FILE = os.environ.get("WORDS_FILE", "words.json")

# ── Default game settings ──────────────────────────────────────────────────
DEFAULT_NUM_QUESTIONS  = 10
DEFAULT_TIME_PER_ROUND = 90   # seconds

# ── Scoring ────────────────────────────────────────────────────────────────
CORRECT_POINTS = 5
HINT_COST      = 2   # deducted from session score only
SKIP_COST      = 1   # deducted from session score only

# ── Supported levels ────────────────────────────────────────────────────────
LEVELS = ["A1", "A2", "B1"]

# ── Answer-matching ─────────────────────────────────────────────────────────
# Levenshtein similarity threshold (0.0–1.0).
# 0.82 accepts 1–2 typos on most words; raise toward 1.0 to be stricter.
FUZZY_THRESHOLD: float = 0.82

# ── Access control ──────────────────────────────────────────────────────────
# Allow /startgame in private DM chats (one-person solo practice).
# Set to True once you're comfortable opening the bot beyond group control.
ALLOW_PRIVATE_GAMES: bool = False
