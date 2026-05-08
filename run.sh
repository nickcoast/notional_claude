#!/bin/sh
# Start the IB Portfolio dashboard.
# TWS must already be running and accepting API connections on port 7497.

HOST="${IB_DASHBOARD_HOST:-127.0.0.1}"
PORT="${IB_DASHBOARD_PORT:-8000}"

case "${1:-}" in
    --lan)
        HOST="0.0.0.0"
        ;;
    -h|--help)
        echo "Usage: ./run.sh [--lan]"
        echo
        echo "  --lan    Listen on all network interfaces for same-LAN devices."
        echo
        echo "Environment:"
        echo "  IB_DASHBOARD_HOST  Bind host (default: 127.0.0.1)"
        echo "  IB_DASHBOARD_PORT  Bind port (default: 8000)"
        exit 0
        ;;
    "")
        ;;
    *)
        echo "Unknown option: $1" >&2
        echo "Usage: ./run.sh [--lan]" >&2
        exit 2
        ;;
esac

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $PORT is already in use. The dashboard may already be running." >&2
    echo "Open http://$HOST:$PORT or stop the existing process before running ./run.sh again." >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
    exit 1
fi

if [ "$HOST" = "0.0.0.0" ]; then
    echo "Listening on all interfaces."
    if command -v ipconfig >/dev/null 2>&1; then
        WIFI_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
        if [ -n "$WIFI_IP" ]; then
            echo "Same-network devices can try: http://$WIFI_IP:$PORT"
        fi
    fi
    echo "Mac-local URL: http://127.0.0.1:$PORT"
else
    echo "Mac-local URL: http://$HOST:$PORT"
fi

exec uvicorn api:app --host "$HOST" --port "$PORT"
