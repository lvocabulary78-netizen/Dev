#!/usr/bin/env python3

import sys
import os
import logging

import telebot
from flask import Flask, request, abort

from config import BOT_TOKEN
from db import init_db
from word_cache import load_words
import handlers

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Bot + Flask setup ──────────────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ── Health check ───────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return "OK", 200

# ── Webhook endpoint (FIXED) ───────────────────────────────────────────────
@app.route("/webhook/<token>", methods=["POST"])
def receive_update(token):
    # 🔐 Validate token
    if token != BOT_TOKEN:
        abort(403)

    if request.content_type != "application/json":
        abort(403)

    # 🔍 Debug log (VERY important)
    logger.info("Webhook hit!")

    update = telebot.types.Update.de_json(
        request.get_data(as_text=True)
    )
    bot.process_new_updates([update])

    return "OK", 200

# ── Startup logic ──────────────────────────────────────────────────────────
def setup_webhook():
    render_url = os.environ.get("RENDER_APP_URL", "").rstrip("/")

    if not render_url:
        logger.critical(
            "RENDER_APP_URL is not set. "
            "Set it in Render environment variables."
        )
        sys.exit(1)

    webhook_url = f"{render_url}/webhook/{BOT_TOKEN}"

    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)

    logger.info("Webhook set to %s", webhook_url)

def run_polling():
    logger.warning("Running in polling mode (LOCAL ONLY)")
    bot.remove_webhook()
    bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)

def main():
    init_db()
    load_words()
    handlers.register(bot)

    logger.info("All handlers registered.")

    if "--polling" in sys.argv:
        run_polling()
    else:
        setup_webhook()

if __name__ == "__main__":
    main()