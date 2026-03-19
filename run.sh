#!/bin/sh
# Start the IB Portfolio dashboard.
# TWS must already be running and accepting API connections on port 7497.
exec uvicorn api:app --host 127.0.0.1 --port 8000
