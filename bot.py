#!/usr/bin/env python3

import sys
import os
import logging

import telebot
from flask import Flask, request, abort

# ── 🔥 FALLBACK VALUES (EDIT THESE) ────────────────────────────────────────
FALLBACK_TOKEN = "8732231617:AAFoiNC-Vg0M-9Kai23yMnlP7xuRGvAXf0M"
FALLBACK_URL   = "https://dev-pazd.onrender.com"
# ─────────────────────────────────────────────────────────────────────────

# Try env vars first, fallback if missing
BOT_TOKEN = os.environ.get("BOT_TOKEN", FALLBACK_TOKEN)
RENDER_APP_URL = os.environ.get("RENDER_APP_URL", FALLBACK_URL)

from db import init_db
from word_cache import load_words
import handlers

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Bot + Flask setup ─────────────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ── Health check ──────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return "OK", 200

# ── Webhook endpoint ──────────────────────────────────────────────────────
@app.route("/webhook/<token>", methods=["POST"])
def receive_update(token):
    if token != BOT_TOKEN:
        abort(403)

    if request.content_type != "application/json":
        abort(403)

    logger.info("🔥 Webhook hit!")

    update = telebot.types.Update.de_json(
        request.get_data(as_text=True)
    )
    bot.process_new_updates([update])

    return "OK", 200

# ── Initialization (important for gunicorn) ───────────────────────────────
init_db()
load_words()
handlers.register(bot)

logger.info("All handlers registered.")

# ── Webhook setup ─────────────────────────────────────────────────────────
def setup_webhook():
    render_url = RENDER_APP_URL.rstrip("/")

    if not render_url or "PUT_YOUR" in BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN or RENDER_APP_URL not set correctly")
        sys.exit(1)

    webhook_url = f"{render_url}/webhook/{BOT_TOKEN}"

    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)

    logger.info("✅ Webhook set to %s", webhook_url)

# Run on startup
setup_webhook()

# ── Local polling fallback ────────────────────────────────────────────────
if __name__ == "__main__":
    if "--polling" in sys.argv:
        logger.warning("Running polling mode")
        bot.remove_webhook()
        bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)