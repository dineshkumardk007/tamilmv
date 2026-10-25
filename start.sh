#!/bin/sh
set -e

echo "[Startup] Starting WebTorrent streaming engine on port 8778..."
node streamer.js &

sleep 2

echo "[Startup] Starting FastAPI web application on port ${PORT:-8000}..."
exec uvicorn web:app --host 0.0.0.0 --port "${PORT:-8000}"
