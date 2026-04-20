"""
game_logic.py — Self-contained GameSession class.

Handles all in-memory game state: rounds, scoring, teams, timers.
No I/O — the handlers layer is responsible for sending Telegram messages
and persisting results to the database.
"""

import random
import threading
import re
import logging
from difflib import SequenceMatcher
from config import (
    CORRECT_POINTS, HINT_COST, SKIP_COST,
    DEFAULT_NUM_QUESTIONS, DEFAULT_TIME_PER_ROUND,
    FUZZY_THRESHOLD,
)

logger = logging.getLogger(__name__)

# Try to import fast Levenshtein, fallback to difflib
try:
    from Levenshtein import ratio as levenshtein_ratio
    logger.info("Using python-Levenshtein for fuzzy matching.")
except ImportError:
    logger.warning("python-Levenshtein not installed; using difflib (slower). "
                   "Add it to requirements.txt for better performance.")
    def levenshtein_ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()


class GameSession:
    """
    Represents one active game in a single Telegram chat.

    Modes:
        "individual" — Everyone races to answer; scores are tracked per person.
        "team"       — Players are pre-assigned to teams; scores are shared.

    Lifecycle:
        WAITING ──(next_round)──► RUNNING ──(all rounds done)──► FINISHED
    """

    STATE_WAITING  = "waiting"
    STATE_RUNNING  = "running"
    STATE_FINISHED = "finished"

    # ── Construction ──────────────────────────────────────────────────────

    def __init__(
        self,
        group_id:  int,
        mode:      str,       # "individual" | "team"
        level:     str,
        category:  str,
        words:     list[dict],
        settings:  dict,
    ) -> None:
        self.group_id = group_id
        self.mode     = mode
        self.level    = level
        self.category = category
        self.state    = self.STATE_WAITING

        # ── Settings ──────────────────────────────────────────────────────
        n = settings.get("num_questions", DEFAULT_NUM_QUESTIONS)
        self.num_questions    = min(n, len(words))
        self.time_per_round   = settings.get("time_per_round", DEFAULT_TIME_PER_ROUND)
        self.hints_enabled    = bool(settings.get("hints_enabled", 1))
        self.skip_enabled     = bool(settings.get("skip_enabled",  1))

        # ── Word list ─────────────────────────────────────────────────────
        self._words: list[dict] = random.sample(words, self.num_questions)
        self._index: int = -1
        self.current_word: dict | None = None

        # ── Players  {user_id: {"name": str, "points": int}} ──────────────
        self.players: dict[int, dict] = {}

        # ── Teams  {team_id: {"name": str, "members": list[int], "points": int}}
        self.teams: dict[str, dict] = {}

        # ── Round state ────────────────────────────────────────────────────
        self._round_lock    = threading.Lock()
        self._round_claimed = False

        # Progressive hint state (reset each round)
        self._hints_shown: int = 0

        # ── Timer ─────────────────────────────────────────────────────────
        self._timer: threading.Timer | None = None

    # ── Player / Team management ─────────────────────────────────────────

    def add_player(self, user_id: int, name: str) -> None:
        if user_id not in self.players:
            self.players[user_id] = {"name": name, "points": 0}

    def remove_player(self, user_id: int) -> None:
        self.players.pop(user_id, None)

    def assign_teams_random(self) -> None:
        """
        Randomly pair all joined players into teams of 2.
        If the count is odd, the last team gets 3 members instead of
        leaving one person as a solo team.
        """
        ids = list(self.players.keys())
        random.shuffle(ids)
        self.teams.clear()
        team_num = 1
        i = 0
        while i < len(ids):
            remaining = len(ids) - i
            # If exactly 3 left, make a trio; otherwise pair normally
            chunk = 3 if remaining == 3 else 2
            members = ids[i : i + chunk]
            i += chunk
            names   = [self.players[uid]["name"] for uid in members]
            team_id = f"team_{team_num}"
            self.teams[team_id] = {
                "name":    f"Team {team_num} ({' & '.join(names)})",
                "members": members,
                "points":  0,
            }
            team_num += 1

    def get_player_team(self, user_id: int) -> str | None:
        for tid, team in self.teams.items():
            if user_id in team["members"]:
                return tid
        return None

    # ── Round control ─────────────────────────────────────────────────────

    def next_round(self) -> bool:
        """
        Advance to the next word.
        Returns True if a new round started, False if the game is over.
        """
        self._index += 1
        if self._index >= len(self._words):
            self.state = self.STATE_FINISHED
            return False
        self.current_word   = self._words[self._index]
        self.state          = self.STATE_RUNNING
        self._round_claimed = False
        self._hints_shown   = 0          # reset progressive hint counter
        return True

    def claim_round(self) -> bool:
        """
        Thread-safe claim: first caller (player answer OR timeout) wins.
        Returns True if this call successfully claimed the round.
        """
        with self._round_lock:
            if self._round_claimed:
                return False
            self._round_claimed = True
            return True

    # ── Answer validation ─────────────────────────────────────────────────

    @staticmethod
    def _normalize(text: str) -> str:
        """Lowercase, strip punctuation, collapse whitespace."""
        text = text.lower().strip()
        text = re.sub(r"[^\w\s]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def check_answer(self, answer: str) -> bool:
        """
        Case-insensitive, punctuation-stripped, typo-tolerant synonym check.

        Improvements vs original:
          • Relative length guard (min/max ratio) instead of absolute ±2,
            which was too strict for multi-word synonyms (e.g. "have a meal").
          • Short synonyms (≤ 3 chars) require exact match — fuzzy matching
            on tiny words produces too many false positives.
          • Threshold constant imported from config (default 0.82).
          • Fixed comment: threshold is 0.82, not "85%".
        """
        if not self.current_word:
            return False

        cleaned = self._normalize(answer)
        if not cleaned:
            return False

        valid_synonyms = [
            self._normalize(s)
            for s in self.current_word.get("synonyms", [])
        ]
        valid_synonyms = [s for s in valid_synonyms if s]

        # 1. Exact match (after normalisation)
        if cleaned in valid_synonyms:
            return True

        # 2. Fuzzy match — skip very short synonyms to avoid false positives
        for syn in valid_synonyms:
            if len(syn) <= 3:
                # "go", "am", "is" etc. — require exact match only
                continue

            # Relative length guard: skip if one string is less than 55% the
            # length of the other.  This filters out things like "consume"
            # vs "c", while still allowing dropped articles ("have meal" vs
            # "have a meal" → 9/10 = 0.9 ✓).
            min_len = min(len(cleaned), len(syn))
            max_len = max(len(cleaned), len(syn))
            if max_len > 0 and (min_len / max_len) < 0.55:
                continue

            ratio = levenshtein_ratio(cleaned, syn)
            if ratio >= FUZZY_THRESHOLD:
                logger.info(
                    "Fuzzy match accepted: %r → %r (ratio %.2f, threshold %.2f)",
                    answer, syn, ratio, FUZZY_THRESHOLD,
                )
                return True

        return False

    # ── Scoring ───────────────────────────────────────────────────────────

    def award_points(self, user_id: int, points: int) -> None:
        """Add points to the player (and their team in team mode)."""
        if user_id in self.players:
            self.players[user_id]["points"] += points
        if self.mode == "team":
            tid = self.get_player_team(user_id)
            if tid and tid in self.teams:
                self.teams[tid]["points"] += points

    def deduct_points(self, user_id: int, points: int) -> None:
        """
        Deduct session points (hint/skip cost).

        Intentionally allows negative scores — that IS the penalty.
        The old floor-at-zero meant hints were free when you had 0 pts,
        since subtracting from nothing kept you at 0.  Now:
            3 hints (−6) + correct answer (+5) = −1 net  ← real cost
        """
        if user_id in self.players:
            self.players[user_id]["points"] -= points
        if self.mode == "team":
            tid = self.get_player_team(user_id)
            if tid and tid in self.teams:
                self.teams[tid]["points"] -= points

    def get_leaderboard(self) -> list[tuple]:
        """
        Returns sorted leaderboard entries.
        Individual → sorted list of (user_id, player_dict)
        Team       → sorted list of (team_id, team_dict)
        """
        if self.mode == "team":
            return sorted(
                self.teams.items(),
                key=lambda x: x[1]["points"],
                reverse=True,
            )
        return sorted(
            self.players.items(),
            key=lambda x: x[1]["points"],
            reverse=True,
        )

    # ── Progressive hint ──────────────────────────────────────────────────

    def get_hint(self) -> tuple[str, int, int]:
        """
        Reveal one additional letter of the first synonym per call.
        Subsequent calls (by anyone) reveal one more letter each time.

        Returns:
            (display_string, letters_revealed, total_letters)

        Example for "joyful" after 2 calls:
            "J O _ _ _ _"  (2 revealed, 6 total)

        For multi-word synonyms the display preserves letter groupings
        visually so players can see word boundaries once enough is shown.
        """
        if not self.current_word or not self.current_word.get("synonyms"):
            return ("?", 0, 0)

        syn = self.current_word["synonyms"][0]
        # Count only non-space characters
        letter_positions = [i for i, c in enumerate(syn) if c != " "]
        total_letters    = len(letter_positions)

        if total_letters == 0:
            return ("?", 0, 0)

        # Increment reveal counter, capped at total letters
        self._hints_shown = min(self._hints_shown + 1, total_letters)
        n = self._hints_shown

        # Build display string: reveal first n non-space chars; keep spaces
        result      = []
        revealed    = 0
        for c in syn:
            if c == " ":
                result.append(" ")
            elif revealed < n:
                result.append(c.upper())
                revealed += 1
            else:
                result.append("_")

        # Space out chars for readability: "J O Y _ _ _"
        spaced = " ".join(result)
        return (spaced, n, total_letters)

    @property
    def hints_shown(self) -> int:
        return self._hints_shown

    # ── Timer ─────────────────────────────────────────────────────────────

    def start_timer(self, on_expire) -> None:
        """Start (or restart) the round timer."""
        self.cancel_timer()
        self._timer = threading.Timer(self.time_per_round, on_expire)
        self._timer.daemon = True
        self._timer.start()

    def cancel_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    # ── Convenience properties ────────────────────────────────────────────

    @property
    def round_number(self) -> int:
        return self._index + 1

    @property
    def total_rounds(self) -> int:
        return len(self._words)

    def __repr__(self) -> str:
        return (
            f"<GameSession group={self.group_id} mode={self.mode} "
            f"level={self.level} cat={self.category} "
            f"round={self.round_number}/{self.total_rounds} state={self.state}>"
        )
