#!/usr/bin/env python3
"""
bot.py — Entry point for the Synonym Game Bot.

─── Why webhooks, not polling ────────────────────────────────────────────────
Polling (infinity_polling) requires a persistent outbound connection and
conflicts if two instances run simultaneously (Render's zero-downtime deploys
briefly spin up a new instance before killing the old one → 409 Conflict).

Webhooks flip the model: Telegram POSTs each update to YOUR URL the moment
it arrives.  No persistent connection, no conflict, works perfectly with
Render's free Web Service tier.

─── How it works ─────────────────────────────────────────────────────────────
1. On startup, we call bot.set_webhook(WEBHOOK_URL).
2. Telegram sends every update as an HTTP POST to that URL.
3. Flask receives it, hands it to pyTelegramBotAPI, done.
4. The secret token in the URL path keeps random people from faking updates.

─── Required environment variables ───────────────────────────────────────────
  BOT_TOKEN        — from BotFather (required)
  RENDER_APP_URL   — your Render URL, e.g. https://my-bot.onrender.com
                     Set this in Render → Environment.
                     No trailing slash.

Optional:
  DB_PATH          — default: synonym_game.db
  WORDS_FILE       — default: words.json
  PORT             — default: 8000 (Render sets this automatically)

─── Local development ────────────────────────────────────────────────────────
Webhooks require a public HTTPS URL, so you can't use them locally without
a tunnel.  For local testing use polling mode:

    export BOT_TOKEN="your_token"
    python bot.py --polling

The --polling flag switches to infinity_polling() for local use only.
Never use --polling while the Render service is also running.
"""

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

bot  = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app  = Flask(__name__)

# The webhook path contains the token so random POST requests are ignored.
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"


@app.route("/health")
def health():
    """Render uses this to confirm the service is alive."""
    return "OK", 200


@app.route(WEBHOOK_PATH, methods=["POST"])
def receive_update():
    """Telegram delivers every update here as a JSON POST."""
    if request.content_type != "application/json":
        abort(403)
    update = telebot.types.Update.de_json(request.get_data(as_text=True))
    bot.process_new_updates([update])
    return "OK", 200


# ── Entry point ────────────────────────────────────────────────────────────

def run_webhook() -> None:
    """Production mode: webhook via Flask."""
    render_url = os.environ.get("RENDER_APP_URL", "").rstrip("/")
    if not render_url:
        logger.critical(
            "RENDER_APP_URL is not set. "
            "Add it in Render → your service → Environment. "
            "Example: https://my-bot.onrender.com"
        )
        sys.exit(1)

    webhook_url = render_url + WEBHOOK_PATH

    # Remove any old webhook / polling session before setting the new one.
    bot.remove_webhook()
    bot.set_webhook(url=webhook_url)
    logger.info("Webhook set to %s", webhook_url)

    port = int(os.environ.get("PORT", 8000))
    logger.info("Flask listening on port %d", port)
    # use_reloader=False is critical — reloader spawns a second process which
    # would try to set the webhook again and cause the exact 409-style conflict
    # we're trying to avoid.
    from gunicorn.app.base import BaseApplication

    class _StandaloneApp(BaseApplication):
        def __init__(self, app, options=None):
            self.options = options or {}
            self.application = app
            super().__init__()
        def load_config(self):
            for k, v in self.options.items():
                self.cfg.set(k.lower(), v)
        def load(self):
            return self.application

    options = {
        "bind": f"0.0.0.0:{port}",
        "workers": 1,         # 1 worker — SQLite + in-memory state must not be forked
        "threads": 4,         # 4 threads handle concurrent webhook POSTs within that worker
        "timeout": 120,       # give game timers enough room
        "loglevel": "info",
    }
    logger.info("Starting gunicorn on port %d (1 worker, 4 threads)", port)
    _StandaloneApp(app, options).run()


def run_polling() -> None:
    """Local development mode: long polling (no public URL needed)."""
    logger.warning(
        "Running in POLLING mode — for local development only. "
        "Do NOT use this while the Render service is also running."
    )
    bot.remove_webhook()
    bot.infinity_polling(timeout=60, long_polling_timeout=30, skip_pending=True)


def main() -> None:
    init_db()
    load_words()
    handlers.register(bot)
    logger.info("All handlers registered.")

    if "--polling" in sys.argv:
        run_polling()
    else:
        run_webhook()


if __name__ == "__main__":
    main()