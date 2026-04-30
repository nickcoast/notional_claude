#!/bin/sh
# Start the IB Portfolio dashboard.
# TWS must already be running and accepting API connections on port 7497.

HOST="${IB_DASHBOARD_HOST:-127.0.0.1}"
PORT="${IB_DASHBOARD_PORT:-8000}"

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $PORT is already in use. The dashboard may already be running." >&2
    echo "Open http://$HOST:$PORT or stop the existing process before running ./run.sh again." >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    exit 1
fi

exec uvicorn api:app --host "$HOST" --port "$PORT"
