#!/usr/bin/env python3
"""
bot.py — Entry point for the Synonym Game Bot.

Deployment (Render / any platform):
    • Set BOT_TOKEN as an environment variable.
    • Deploy as a "Background Worker" on Render (no port binding needed),
      OR keep as a Web Service — the embedded health server below handles
      Render's port scan automatically on $PORT (default 8000).

Local usage:
    export BOT_TOKEN="your_token"
    python bot.py
"""

import os
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

import telebot

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


# ── Minimal health-check server (keeps Render Web Service happy) ───────────

class _HealthHandler(BaseHTTPRequestHandler):
    """Tiny HTTP handler — just returns 200 OK for any GET request."""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *_args):
        pass   # suppress per-request access logs


def _start_health_server() -> None:
    """
    Bind to $PORT (default 8000) in a daemon thread.

    This is only needed when deploying as a Render **Web Service**.
    If you switch the Render service type to **Background Worker**, you
    can remove this function entirely — background workers don't scan for
    open ports.
    """
    port = int(os.environ.get("PORT", 8000))
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info("Health-check server listening on port %d", port)
    except OSError as exc:
        logger.warning("Could not start health server on port %d: %s", port, exc)


# ── Main ───────────────────────────────────────────────────────────────────

def main() -> None:
    # 1. Set up the database
    init_db()

    # 2. Load word cache into memory
    load_words()

    # 3. Start health server (satisfies Render's port-scan requirement)
    _start_health_server()

    # 4. Create and configure the bot
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
    bot.remove_webhook()   # clear any leftover webhook

    # 5. Register all handlers
    handlers.register(bot)
    logger.info("All handlers registered. Starting polling…")

    # 6. Run
    bot.infinity_polling(timeout=60, long_polling_timeout=30)


if __name__ == "__main__":
    main()
