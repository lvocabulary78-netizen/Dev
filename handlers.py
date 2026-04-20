"""
handlers.py — All Telegram message and callback-query handlers.

Key design notes:
  • active_games maps group_id → GameSession (running game)
    or group_id → dict (pending setup state).
  • _get_session() only returns a real GameSession, never a setup dict.
  • All timer callbacks use claim_round() to avoid double-firing.
  • _safe_reply / _safe_send wrap every Telegram API call so a deleted
    message never crashes the bot (fixes the 400 "message to be replied
    not found" error that appeared in the error log).
  • "solo" mode renamed to "individual" — all group members race to
    answer individually. True single-player practice is possible in DM
    when ALLOW_PRIVATE_GAMES is True in config.
  • Super-admins (SUPER_ADMIN_IDS) can use every admin command even when
    they are not a Telegram group admin.
  • /begin can be called by the game initiator OR a group/super admin.
  • Progressive hints: each /hint call reveals one more letter.
  • _games_lock guards the read-modify-write pattern on active_games to
    prevent two simultaneous /startgame commands racing each other.
"""

import threading
import logging
from typing import Union

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    Message, CallbackQuery,
)

import db
import word_cache
from game_logic import GameSession
from config import (
    CORRECT_POINTS, HINT_COST, SKIP_COST, SUPER_ADMIN_IDS,
    DEFAULT_NUM_QUESTIONS, DEFAULT_TIME_PER_ROUND,
    ALLOW_PRIVATE_GAMES,
)

logger = logging.getLogger(__name__)

# ── Module-level state ─────────────────────────────────────────────────────
active_games: dict[int, Union[GameSession, dict]] = {}
_games_lock  = threading.Lock()   # guards read-modify-write on active_games

_bot: telebot.TeleBot | None = None   # set in register()


# ══════════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_session(group_id: int) -> GameSession | None:
    """Return the GameSession for a group, or None if not active."""
    obj = active_games.get(group_id)
    return obj if isinstance(obj, GameSession) else None


def _is_group_admin(chat_id: int, user_id: int) -> bool:
    try:
        admins = _bot.get_chat_administrators(chat_id)
        return any(a.user.id == user_id for a in admins)
    except Exception:
        return False


def _is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS


def _is_admin_or_super(chat_id: int, user_id: int) -> bool:
    """True if the user is a Telegram group admin OR a global super-admin."""
    return _is_super_admin(user_id) or _is_group_admin(chat_id, user_id)


# ── Safe Telegram API wrappers ─────────────────────────────────────────────
# These prevent a deleted/forwarded message from crashing the bot with a
# 400 "message to be replied not found" error.

def _safe_reply(msg: Message, text: str, **kwargs) -> None:
    """
    Try reply_to; if the original message is gone, fall back to send_message.
    If both fail (e.g. bot kicked from group), just log and continue.
    """
    try:
        _bot.reply_to(msg, text, **kwargs)
    except telebot.apihelper.ApiTelegramException as e:
        if "message to be replied not found" in str(e) or \
           "MESSAGE_ID_INVALID" in str(e):
            try:
                _bot.send_message(msg.chat.id, text, **kwargs)
            except Exception as inner:
                logger.warning("_safe_reply fallback failed: %s", inner)
        else:
            logger.warning("_safe_reply failed: %s", e)
    except Exception as e:
        logger.warning("_safe_reply unexpected error: %s", e)


def _safe_send(chat_id: int, text: str, **kwargs) -> None:
    """send_message that logs instead of crashing on API errors."""
    try:
        _bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.warning("_safe_send to %s failed: %s", chat_id, e)


# ── Keyboard builders ──────────────────────────────────────────────────────

def _level_kbd() -> InlineKeyboardMarkup:
    kbd = InlineKeyboardMarkup()
    kbd.row(
        InlineKeyboardButton("🟢 A1 — Beginner",      callback_data="act_level:A1"),
        InlineKeyboardButton("🟡 A2 — Elementary",    callback_data="act_level:A2"),
    )
    kbd.add(InlineKeyboardButton("🟠 B1 — Intermediate", callback_data="act_level:B1"))
    return kbd


def _category_kbd(level: str) -> InlineKeyboardMarkup:
    cats = word_cache.get_categories(level)
    kbd  = InlineKeyboardMarkup(row_width=2)
    btns = [InlineKeyboardButton(c, callback_data=f"cat:{c}") for c in cats]
    kbd.add(*btns)
    return kbd


# ── Messaging helpers ───────────────────────────────────────────────────────

def _send_word(group_id: int, session: GameSession) -> None:
    """Announce the current word and start the round timer."""
    w = session.current_word
    emoji = w.get("emoji") or "🔤"
    pronunciation = w.get("pronunciation")
    pronun_line = f"\n🔊 <i>/{pronunciation}/</i>" if pronunciation else ""
    msg = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📖 <b>Round {session.round_number} / {session.total_rounds}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{emoji} Find a synonym for:\n\n"
        f"<b>  ≫  {w['word'].upper()}  ≪</b>"
        f"{pronun_line}\n\n"
        f"⏱ <b>{session.time_per_round}s</b> on the clock!\n"
    )
    if session.hints_enabled or session.skip_enabled:
        tips = []
        if session.hints_enabled:
            tips.append(f"/hint  (−{HINT_COST} pts, reveals next letter)")
        if session.skip_enabled:
            tips.append(f"/skip  (−{SKIP_COST} pt)")
        msg += "\n💡 " + "  |  ".join(tips)

    _safe_send(group_id, msg)
    session.start_timer(lambda: _handle_timeout(group_id))


def _send_round_result(
    group_id: int,
    session:  GameSession,
    winner:   str | None = None,
) -> None:
    """Reveal the answer details after a round ends."""
    w        = session.current_word
    synonyms = " / ".join(w["synonyms"])

    # ── Examples (bilingual: "EN\nAR" per entry in new format) ─────────────
    examples = w.get("examples", [])
    if not examples and "example" in w:
        examples = [w["example"]]

    if examples:
        # Show only the first example to keep the message concise
        example_text = f"💬 <b>Example:</b>\n<i>{examples[0]}</i>"
    else:
        example_text = ""

    # ── New optional fields ─────────────────────────────────────────────────
    fact         = w.get("fact")
    collocations = w.get("collocations", [])
    proverb      = w.get("proverb")

    header = (
        f"✅ <b>{winner}</b> got it! +{CORRECT_POINTS} pts"
        if winner else
        "⏰ Time's up — no one answered."
    )

    parts = [
        header,
        "",
        f"📚 <b>Word:</b>      {w['word']}",
        f"✔️ <b>Synonyms:</b>  {synonyms}",
        f"🇸🇦 <b>Arabic:</b>    {w['arabic']}",
    ]

    if example_text:
        parts.append(example_text)

    if collocations:
        parts.append(
            "🔗 <b>Collocations:</b>  " + "  •  ".join(collocations)
        )

    if fact:
        parts.append(f"🧠 <b>Did you know?</b>  {fact}")

    if proverb:
        parts.append(f"📜 <b>Proverb:</b>\n<i>{proverb}</i>")

    _safe_send(group_id, "\n".join(parts))


def _send_final_leaderboard(group_id: int, session: GameSession) -> None:
    """Send the end-of-game leaderboard and persist stats."""
    entries = session.get_leaderboard()

    if not entries:
        _safe_send(group_id, "🎮 Game over! No scores to show.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines  = ["🏆 <b>Final Leaderboard</b> 🏆\n"]

    winner_ids: list[int] = []
    for i, (uid_or_tid, info) in enumerate(entries):
        medal = medals[i] if i < 3 else f"  {i + 1}."
        pts   = info["points"]
        lines.append(f"{medal} <b>{info['name']}</b> — {pts} pts")
        if i == 0:
            winner_ids = info.get("members", [uid_or_tid]) \
                         if session.mode == "team" else [uid_or_tid]

    _safe_send(group_id, "\n".join(lines))

    # Persist stats
    for uid, pinfo in session.players.items():
        pts = pinfo["points"]
        won = uid in winner_ids
        db.record_game_result(uid, pts, won)


# ── Round / game flow ──────────────────────────────────────────────────────

def _advance(group_id: int) -> None:
    """Move to the next round, or end the game."""
    session = _get_session(group_id)
    if not session:
        return

    if session.next_round():
        _send_word(group_id, session)
    else:
        _safe_send(group_id, "🎉 <b>Game Over!</b>")
        _send_final_leaderboard(group_id, session)
        with _games_lock:
            active_games.pop(group_id, None)


def _handle_timeout(group_id: int) -> None:
    """Called by the round timer when time expires."""
    session = _get_session(group_id)
    if not session or session.state != GameSession.STATE_RUNNING:
        return
    if not session.claim_round():
        return   # a player already answered

    _send_round_result(group_id, session, winner=None)
    threading.Timer(2.0, _advance, args=(group_id,)).start()


def _start_first_round(group_id: int) -> None:
    """Delayed kick-off so players can read the game announcement."""
    session = _get_session(group_id)
    if not session:
        return
    if session.next_round():
        _send_word(group_id, session)


# ── Private-chat guard helper ──────────────────────────────────────────────

def _check_game_chat(msg: Message) -> bool:
    """
    Returns True if this chat is allowed to run a game.
    - Group/supergroup: always allowed (if activated).
    - Private chat: allowed only when ALLOW_PRIVATE_GAMES is True.
    Sends an error message and returns False otherwise.
    """
    if msg.chat.type in ("group", "supergroup"):
        return True
    if ALLOW_PRIVATE_GAMES:
        return True
    _safe_reply(
        msg,
        "⚠️ Games can only be played in groups.\n"
        "Ask your admin to add the bot to a group and run /activate."
    )
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  Handler registration
# ══════════════════════════════════════════════════════════════════════════════

def register(bot: telebot.TeleBot) -> None:
    global _bot
    _bot = bot

    # ── /start  /help ──────────────────────────────────────────────────────
    @bot.message_handler(commands=["start", "help"])
    def cmd_help(msg: Message):
        is_group = msg.chat.type in ("group", "supergroup")
        private_note = (
            "\n⚠️ Games must be played inside a group."
            if not is_group and not ALLOW_PRIVATE_GAMES else ""
        )
        _safe_send(
            msg.chat.id,
            "🎓 <b>Synonym Game Bot</b>\n\n"
            "A vocabulary game for English learners — guess the synonym!\n"
            + private_note + "\n\n"
            "<b>Player Commands</b>\n"
            "▸ /startgame   — Start an Individual-mode game\n"
            "▸ /startteam   — Start a Team-mode game (join phase)\n"
            "▸ /join        — Join a pending team game\n"
            "▸ /begin       — Begin the team game (initiator or admin)\n"
            "▸ /hint        — Reveal next letter (costs pts; anyone can call)\n"
            "▸ /skip        — Skip the word (costs pts)\n"
            "▸ /leaderboard — Global leaderboard\n"
            "▸ /mystats     — Your personal stats\n\n"
            "<b>Admin Commands</b>\n"
            "▸ /activate          — Activate bot & set level\n"
            "▸ /settings          — View current settings\n"
            "▸ /setquestions [n]  — Questions per game\n"
            "▸ /settime [n]       — Seconds per round\n"
            "▸ /togglehint        — Enable/disable hints\n"
            "▸ /toggleskip        — Enable/disable skip\n"
            "▸ /toggleapproval    — Require admin approval to start\n"
            "▸ /stopgame          — Force-stop active game\n"
            "▸ /ban [user_id]     — Ban a user\n"
            "▸ /unban [user_id]   — Unban a user\n\n"
            "<b>Modes explained</b>\n"
            "🏃 <b>Individual</b> (/startgame): everyone in the group races to "
            "answer first — points are tracked per person.\n"
            "👥 <b>Team</b> (/startteam): players join then get randomly paired "
            "into teams; the whole team shares points."
        )

    # ── /activate ──────────────────────────────────────────────────────────
    @bot.message_handler(commands=["activate"])
    def cmd_activate(msg: Message):
        if msg.chat.type not in ("group", "supergroup"):
            _safe_reply(msg, "⚠️ Use this command inside a group.")
            return
        if not _is_admin_or_super(msg.chat.id, msg.from_user.id):
            _safe_reply(msg, "🚫 Only group admins can activate the bot.")
            return
        _safe_send(
            msg.chat.id,
            "🎯 <b>Activate Synonym Game Bot</b>\n\n"
            "Select the vocabulary level for this group:",
            reply_markup=_level_kbd()
        )

    # ── /startgame ─────────────────────────────────────────────────────────
    @bot.message_handler(commands=["startgame"])
    def cmd_startgame(msg: Message):
        if not _check_game_chat(msg):
            return

        gid   = msg.chat.id
        is_group = msg.chat.type in ("group", "supergroup")

        # Private chat shortcut: auto-create a dummy group record if needed
        if not is_group and ALLOW_PRIVATE_GAMES:
            if not db.get_group(gid):
                db.activate_group(gid, "A1", msg.from_user.id)

        group = db.get_group(gid)
        if not group or not group["activated"]:
            _safe_reply(
                msg,
                "❌ This group isn't activated yet. An admin must run /activate first."
            )
            return
        if db.is_banned(msg.from_user.id):
            _safe_reply(msg, "🚫 You are banned from playing.")
            return

        with _games_lock:
            if gid in active_games:
                _safe_reply(msg, "⚠️ A game is already in progress! Use /stopgame first.")
                return

            settings = db.get_game_settings(gid)
            if settings.get("require_approval") and \
               not _is_admin_or_super(gid, msg.from_user.id):
                _safe_reply(msg, "⏳ Admin approval is required to start a game. Ask an admin!")
                return

            db.ensure_user(msg.from_user.id, msg.from_user.first_name)
            active_games[gid] = {
                "_setup":     True,
                "_mode":      "individual",
                "_initiator": msg.from_user.id,
            }

        level = group["level"]
        _safe_send(
            gid,
            f"🏃 <b>Individual Game</b>  |  Level: <b>{level}</b>\n\n"
            f"Everyone in this group can answer — first correct reply wins each round!\n\n"
            f"Pick a category:",
            reply_markup=_category_kbd(level)
        )

    # ── /startteam ─────────────────────────────────────────────────────────
    @bot.message_handler(commands=["startteam"])
    def cmd_startteam(msg: Message):
        if not _check_game_chat(msg):
            return

        gid   = msg.chat.id
        group = db.get_group(gid)

        if not group or not group["activated"]:
            _safe_reply(msg, "❌ Group not activated. An admin must use /activate.")
            return
        if db.is_banned(msg.from_user.id):
            _safe_reply(msg, "🚫 You are banned from playing.")
            return

        uid  = msg.from_user.id
        name = msg.from_user.first_name
        db.ensure_user(uid, name)

        with _games_lock:
            if gid in active_games:
                _safe_reply(msg, "⚠️ A game is already running!")
                return
            active_games[gid] = {
                "_setup":     True,
                "_joining":   True,
                "_mode":      "team",
                "_initiator": uid,
                "_joiners":   {uid: name},
            }

        _safe_send(
            gid,
            f"👥 <b>Team Mode — Join Phase!</b>\n"
            f"Level: <b>{group['level']}</b>\n\n"
            f"▸ Players: type /join to enter.\n"
            f"▸ {name} (or any admin): type /begin when everyone is in.\n\n"
            f"Currently joined:\n• {name}"
        )

    # ── /join ──────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["join"])
    def cmd_join(msg: Message):
        gid   = msg.chat.id
        state = active_games.get(gid)

        if not isinstance(state, dict) or not state.get("_joining"):
            _safe_reply(msg, "❌ No team game is currently accepting players.")
            return
        if db.is_banned(msg.from_user.id):
            _safe_reply(msg, "🚫 You are banned from playing.")
            return

        uid, name = msg.from_user.id, msg.from_user.first_name
        if uid in state["_joiners"]:
            _safe_reply(msg, "✅ You've already joined!")
            return

        state["_joiners"][uid] = name
        db.ensure_user(uid, name)

        player_list = "\n".join(f"• {n}" for n in state["_joiners"].values())
        _safe_send(
            gid,
            f"✅ <b>{name}</b> joined!\n\n"
            f"Players ({len(state['_joiners'])}):\n{player_list}"
        )

    # ── /begin ─────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["begin"])
    def cmd_begin(msg: Message):
        gid   = msg.chat.id
        state = active_games.get(gid)
        uid   = msg.from_user.id

        if not isinstance(state, dict) or not state.get("_joining"):
            _safe_reply(msg, "❌ No team game pending. Use /startteam first.")
            return

        # FIX: allow the game initiator OR any admin/super-admin to begin.
        # Previously only group admins could begin, so a regular player who
        # ran /startteam could never start their own game.
        is_initiator = (uid == state.get("_initiator"))
        if not is_initiator and not _is_admin_or_super(gid, uid):
            _safe_reply(
                msg,
                "🚫 Only the person who started the game or a group admin can begin."
            )
            return

        joiners = state["_joiners"]
        if len(joiners) < 2:
            _safe_reply(msg, "⚠️ Need at least 2 players to start a team game!")
            return

        state["_joining"] = False
        group = db.get_group(gid)
        level = group["level"]

        _safe_send(
            gid,
            f"👥 <b>{len(joiners)} players locked in!</b>\n"
            f"Level: <b>{level}</b>\n\nNow pick a category:",
            reply_markup=_category_kbd(level)
        )

    # ── /hint ──────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["hint"])
    def cmd_hint(msg: Message):
        gid     = msg.chat.id
        session = _get_session(gid)
        if not session or session.state != GameSession.STATE_RUNNING:
            _safe_reply(msg, "❌ No active round right now.")
            return
        if not session.hints_enabled:
            _safe_reply(msg, "💡 Hints are disabled in this group.")
            return

        uid, name = msg.from_user.id, msg.from_user.first_name

        syn           = session.current_word["synonyms"][0]
        total_letters = sum(1 for c in syn if c != " ")

        if session.hints_shown >= total_letters:
            _safe_reply(
                msg,
                f"💡 The full word is already visible: "
                f"<b>{syn.upper()}</b>"
            )
            return

        session.add_player(uid, name)
        session.deduct_points(uid, HINT_COST)
        hint_str, revealed, total = session.get_hint()

        current_pts = session.players[uid]["points"]
        pts_display = (
            f"{current_pts} pts"
            if current_pts >= 0
            else f"<b>{current_pts} pts</b> ⚠️"   # highlight debt
        )

        _safe_send(
            gid,
            f"💡 <b>Hint {revealed}/{total}</b> for "
            f"<b>{session.current_word['word']}</b>:\n\n"
            f"<code>{hint_str}</code>\n\n"
            f"(−{HINT_COST} pts from {name} → now {pts_display})"
        )

    # ── /skip ──────────────────────────────────────────────────────────────
    @bot.message_handler(commands=["skip"])
    def cmd_skip(msg: Message):
        gid     = msg.chat.id
        session = _get_session(gid)
        if not session or session.state != GameSession.STATE_RUNNING:
            _safe_reply(msg, "❌ No active round right now.")
            return
        if not session.skip_enabled:
            _safe_reply(msg, "⏭️ Skipping is disabled in this group.")
            return

        uid, name = msg.from_user.id, msg.from_user.first_name
        if not session.claim_round():
            return   # already claimed

        session.cancel_timer()
        session.add_player(uid, name)
        session.deduct_points(uid, SKIP_COST)

        _safe_send(gid, f"⏭️ <b>{name}</b> skipped the word. (−{SKIP_COST} pt)")
        _send_round_result(gid, session, winner=None)
        threading.Timer(2.0, _advance, args=(gid,)).start()

    # ── /stopgame ──────────────────────────────────────────────────────────
    @bot.message_handler(commands=["stopgame"])
    def cmd_stopgame(msg: Message):
        gid = msg.chat.id
        if not _is_admin_or_super(gid, msg.from_user.id):
            _safe_reply(msg, "🚫 Admins only.")
            return

        with _games_lock:
            obj = active_games.get(gid)
            if not obj:
                _safe_reply(msg, "No active game to stop.")
                return
            if isinstance(obj, GameSession):
                obj.cancel_timer()
            active_games.pop(gid, None)

        _safe_send(gid, "🛑 Game stopped by admin.")

    # ── /leaderboard ───────────────────────────────────────────────────────
    @bot.message_handler(commands=["leaderboard"])
    def cmd_leaderboard(msg: Message):
        entries = db.get_global_leaderboard(10)
        if not entries:
            _safe_reply(msg, "📊 No scores yet — be the first to play!")
            return

        medals = ["🥇", "🥈", "🥉"]
        lines  = ["🌍 <b>Global Leaderboard</b>\n"]
        for i, e in enumerate(entries):
            medal = medals[i] if i < 3 else f"  {i + 1}."
            name  = e.get("username") or f"User {e['user_id']}"
            lines.append(
                f"{medal} <b>{name}</b> — {e['total_points']} pts"
                f"  (W:{e['wins']} L:{e['losses']})"
            )
        _safe_send(msg.chat.id, "\n".join(lines))

    # ── /mystats ───────────────────────────────────────────────────────────
    @bot.message_handler(commands=["mystats"])
    def cmd_mystats(msg: Message):
        uid = msg.from_user.id
        db.ensure_user(uid, msg.from_user.first_name)
        user = db.get_user(uid)
        if not user:
            _safe_reply(msg, "No stats yet — go play!")
            return
        _safe_reply(
            msg,
            f"📊 <b>Your Stats — {msg.from_user.first_name}</b>\n\n"
            f"⭐ Total Points: <b>{user['total_points']}</b>\n"
            f"🏆 Wins:         <b>{user['wins']}</b>\n"
            f"😔 Losses:       <b>{user['losses']}</b>\n"
            f"🚫 Banned:       {'Yes' if user['is_banned'] else 'No'}"
        )

    # ── /settings ──────────────────────────────────────────────────────────
    @bot.message_handler(commands=["settings"])
    def cmd_settings(msg: Message):
        gid = msg.chat.id
        if msg.chat.type not in ("group", "supergroup"):
            _safe_reply(msg, "⚠️ Use in a group.")
            return
        if not _is_admin_or_super(gid, msg.from_user.id):
            _safe_reply(msg, "🚫 Admins only.")
            return

        group = db.get_group(gid)
        level = group["level"] if group else "N/A"
        s     = db.get_game_settings(gid)

        _safe_send(
            gid,
            f"⚙️ <b>Group Settings</b>\n\n"
            f"📚 Level:            <b>{level}</b>  (/activate to change)\n"
            f"❓ Questions/game:   <b>{s['num_questions']}</b>  — /setquestions [n]\n"
            f"⏱ Seconds/round:    <b>{s['time_per_round']}s</b>  — /settime [n]\n"
            f"💡 Hints:            <b>{'On' if s['hints_enabled'] else 'Off'}</b>  — /togglehint\n"
            f"⏭ Skip:             <b>{'On' if s['skip_enabled'] else 'Off'}</b>  — /toggleskip\n"
            f"🔐 Require approval: <b>{'Yes' if s['require_approval'] else 'No'}</b>  — /toggleapproval\n"
        )

    # ── Settings mutators ──────────────────────────────────────────────────

    @bot.message_handler(commands=["setquestions"])
    def cmd_setquestions(msg: Message):
        gid = msg.chat.id
        if not _is_admin_or_super(gid, msg.from_user.id):
            _safe_reply(msg, "🚫 Admins only.")
            return
        parts = msg.text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            _safe_reply(msg, "Usage: /setquestions [number]   e.g. /setquestions 10")
            return
        n = int(parts[1])
        if not 1 <= n <= 50:
            _safe_reply(msg, "Please pick a number between 1 and 50.")
            return
        db.update_game_settings(gid, num_questions=n)
        _safe_reply(msg, f"✅ Questions per game set to <b>{n}</b>.")

    @bot.message_handler(commands=["settime"])
    def cmd_settime(msg: Message):
        gid = msg.chat.id
        if not _is_admin_or_super(gid, msg.from_user.id):
            _safe_reply(msg, "🚫 Admins only.")
            return
        parts = msg.text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            _safe_reply(msg, "Usage: /settime [seconds]   e.g. /settime 60")
            return
        n = int(parts[1])
        if not 10 <= n <= 300:
            _safe_reply(msg, "Please pick a value between 10 and 300 seconds.")
            return
        db.update_game_settings(gid, time_per_round=n)
        _safe_reply(msg, f"✅ Time per round set to <b>{n}s</b>.")

    @bot.message_handler(commands=["togglehint"])
    def cmd_togglehint(msg: Message):
        gid = msg.chat.id
        if not _is_admin_or_super(gid, msg.from_user.id):
            _safe_reply(msg, "🚫 Admins only.")
            return
        s   = db.get_game_settings(gid)
        new = 0 if s["hints_enabled"] else 1
        db.update_game_settings(gid, hints_enabled=new)
        _safe_reply(msg, f"💡 Hints are now <b>{'enabled' if new else 'disabled'}</b>.")

    @bot.message_handler(commands=["toggleskip"])
    def cmd_toggleskip(msg: Message):
        gid = msg.chat.id
        if not _is_admin_or_super(gid, msg.from_user.id):
            _safe_reply(msg, "🚫 Admins only.")
            return
        s   = db.get_game_settings(gid)
        new = 0 if s["skip_enabled"] else 1
        db.update_game_settings(gid, skip_enabled=new)
        _safe_reply(msg, f"⏭️ Skip is now <b>{'enabled' if new else 'disabled'}</b>.")

    @bot.message_handler(commands=["toggleapproval"])
    def cmd_toggleapproval(msg: Message):
        gid = msg.chat.id
        if not _is_admin_or_super(gid, msg.from_user.id):
            _safe_reply(msg, "🚫 Admins only.")
            return
        s   = db.get_game_settings(gid)
        new = 0 if s.get("require_approval") else 1
        db.update_game_settings(gid, require_approval=new)
        _safe_reply(
            msg,
            f"🔐 Admin approval to start games is now "
            f"<b>{'required' if new else 'not required'}</b>."
        )

    # ── /ban  /unban ───────────────────────────────────────────────────────
    @bot.message_handler(commands=["ban"])
    def cmd_ban(msg: Message):
        if not _is_admin_or_super(msg.chat.id, msg.from_user.id):
            _safe_reply(msg, "🚫 Admins only.")
            return
        parts = msg.text.split()
        if len(parts) < 2:
            _safe_reply(msg, "Usage: /ban [user_id]")
            return
        try:
            target = int(parts[1])
        except ValueError:
            _safe_reply(msg, "⚠️ Invalid user ID.")
            return
        db.ban_user(target)
        _safe_reply(msg, f"🔨 User <code>{target}</code> has been banned.")

    @bot.message_handler(commands=["unban"])
    def cmd_unban(msg: Message):
        if not _is_admin_or_super(msg.chat.id, msg.from_user.id):
            _safe_reply(msg, "🚫 Admins only.")
            return
        parts = msg.text.split()
        if len(parts) < 2:
            _safe_reply(msg, "Usage: /unban [user_id]")
            return
        try:
            target = int(parts[1])
        except ValueError:
            _safe_reply(msg, "⚠️ Invalid user ID.")
            return
        db.unban_user(target)
        _safe_reply(msg, f"✅ User <code>{target}</code> has been unbanned.")

    # ══════════════════════════════════════════════════════════════════════
    #  Answer handler — fires on every non-command group/private text
    # ══════════════════════════════════════════════════════════════════════
    @bot.message_handler(
        func=lambda m: (
            m.text
            and not m.text.startswith("/")
            and (
                m.chat.type in ("group", "supergroup")
                or (ALLOW_PRIVATE_GAMES and m.chat.type == "private")
            )
        )
    )
    def handle_answer(msg: Message):
        gid     = msg.chat.id
        session = _get_session(gid)

        if not session or session.state != GameSession.STATE_RUNNING:
            return

        uid, name = msg.from_user.id, msg.from_user.first_name
        if db.is_banned(uid):
            return

        if not session.check_answer(msg.text):
            return   # wrong answer — keep waiting

        # Thread-safe claim: first correct answer wins the round
        if not session.claim_round():
            return   # another answer arrived simultaneously

        session.cancel_timer()
        session.add_player(uid, name)
        session.award_points(uid, CORRECT_POINTS)
        db.ensure_user(uid, name)

        _send_round_result(gid, session, winner=name)
        threading.Timer(2.0, _advance, args=(gid,)).start()

    # ══════════════════════════════════════════════════════════════════════
    #  Callback query handler — inline keyboard responses
    # ══════════════════════════════════════════════════════════════════════
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callback(call: CallbackQuery):
        data = call.data
        gid  = call.message.chat.id
        uid  = call.from_user.id

        # ── Level selection (group activation) ─────────────────────────
        if data.startswith("act_level:"):
            level = data.split(":")[1]
            if not _is_admin_or_super(gid, uid):
                _bot.answer_callback_query(call.id, "🚫 Only admins can do this.")
                return
            db.activate_group(gid, level, uid)
            try:
                _bot.edit_message_text(
                    f"✅ <b>Group activated!</b>\n\n"
                    f"Vocabulary level: <b>{level}</b>\n"
                    f"Word count: <b>{word_cache.word_count(level)}</b>\n\n"
                    f"Players can start with /startgame\n"
                    f"Adjust settings with /settings",
                    chat_id=gid,
                    message_id=call.message.message_id
                )
            except Exception as e:
                logger.warning("edit_message_text failed in act_level: %s", e)
            _bot.answer_callback_query(call.id, f"✅ Activated at level {level}!")

        # ── Category selection ──────────────────────────────────────────
        elif data.startswith("cat:"):
            category = data[4:]
            state    = active_games.get(gid)

            if not isinstance(state, dict) or not state.get("_setup"):
                _bot.answer_callback_query(call.id, "No pending game found.")
                return

            if uid != state["_initiator"]:
                _bot.answer_callback_query(
                    call.id, "Only the person who started the game can pick a category!"
                )
                return

            group    = db.get_group(gid)
            level    = group["level"]
            settings = db.get_game_settings(gid)
            words    = word_cache.get_words(level, category)

            if not words:
                _bot.answer_callback_query(
                    call.id, f"No words found for {level} / {category}!"
                )
                return

            mode    = state["_mode"]
            joiners = state.get("_joiners", {uid: call.from_user.first_name})
            session = GameSession(
                group_id=gid,
                mode=mode,
                level=level,
                category=category,
                words=words,
                settings=settings,
            )

            for join_uid, join_name in joiners.items():
                session.add_player(join_uid, join_name)

            if mode == "team":
                session.assign_teams_random()

            with _games_lock:
                active_games[gid] = session

            mode_label = "👥 Team" if mode == "team" else "🏃 Individual"
            try:
                _bot.edit_message_text(
                    f"🎮 <b>Game Starting!</b>\n\n"
                    f"Mode:     {mode_label}\n"
                    f"Level:    <b>{level}</b>\n"
                    f"Category: <b>{category}</b>\n"
                    f"Words:    <b>{session.total_rounds}</b>\n\n"
                    f"Get ready...",
                    chat_id=gid,
                    message_id=call.message.message_id
                )
            except Exception as e:
                logger.warning("edit_message_text failed in cat: %s", e)

            if mode == "team" and session.teams:
                team_lines = "\n".join(f"• {t['name']}" for t in session.teams.values())
                _safe_send(gid, f"👥 <b>Teams for this game:</b>\n{team_lines}")

            _bot.answer_callback_query(call.id, "Starting!")
            threading.Timer(2.5, _start_first_round, args=(gid,)).start()

        else:
            try:
                _bot.answer_callback_query(call.id)
            except Exception:
                pass
