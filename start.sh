#!/usr/bin/env sh
set -e

PORT="${PORT:-10000}"

# Render ke liye web server (healthcheck / port bind)
python -m http.server "$PORT" --bind 0.0.0.0 >/dev/null 2>&1 &

# Telegram bot (polling)
exec python bot.py
