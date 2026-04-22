#!/usr/bin/env python3

import logging
import telebot
from flask import Flask, request

from db import init_db
from word_cache import load_words
import handlers

# ── CONFIG (NO ENV VARS) ─────────────────────────────
BOT_TOKEN = "8732231617:AAFoiNC-Vg0M-9Kai23yMnlP7xuRGvAXf0M"
BASE_URL  = "https://dev-pazd.onrender.com"
# ─────────────────────────────────────────────────────

# ── Logging ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Init bot + Flask ─────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ── Health check ─────────────────────────────────────
@app.route("/health")
def health():
    return "OK", 200


# ── WEBHOOK (FIXED) ──────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    logger.info("🔥 Webhook hit!")

    try:
        json_str = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        logger.exception("Webhook error: %s", e)

    return "OK", 200


# ── INIT SYSTEMS ─────────────────────────────────────
init_db()
load_words()
handlers.register(bot)

logger.info("All handlers registered.")


# ── SET WEBHOOK ──────────────────────────────────────
def setup_webhook():
    webhook_url = f"{BASE_URL}/webhook"

    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)

    logger.info("✅ Webhook set to %s", webhook_url)


setup_webhook()


# ── LOCAL TEST MODE ───────────────────────────────────
if __name__ == "__main__":
    bot.infinity_polling(skip_pending=True)