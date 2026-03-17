"""
FastAPI backend for the IB Portfolio dashboard.

Run with:
    uvicorn api:app --host 127.0.0.1 --port 8000

Configuration via environment variables:
    IB_POLL_INTERVAL   Seconds between portfolio fetches (default 15, min 10)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from service import DEFAULT_POLL_INTERVAL, IBPollingService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("ib_service.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ib_api")

# ib_insync emits "Can't find EId with tickerId:N" at WARNING level when
# cancelMktData is called for a request IB never acknowledged (common after
# hours).  These are benign; suppress them to keep the log readable.
class _SuppressEIdWarnings(logging.Filter):
    def filter(self, record):
        return "Can't find EId" not in record.getMessage()

logging.getLogger("ib_insync.wrapper").addFilter(_SuppressEIdWarnings())

poll_interval = int(os.getenv("IB_POLL_INTERVAL", str(DEFAULT_POLL_INTERVAL)))
service = IBPollingService(poll_interval=poll_interval)

app = FastAPI(title="IB Portfolio Service")


# ── Jinja2 templates ──────────────────────────────────────────────────────────

templates = Jinja2Templates(directory="templates")


def _filter_currency(value):
    """Format as $1,234,567.89; returns — for None/invalid."""
    if value is None:
        return "—"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _filter_number(value, decimals=2):
    """Format as 1,234.56; returns — for None/invalid."""
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def _filter_timestamp(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "—"


templates.env.filters["currency"] = _filter_currency
templates.env.filters["number"] = _filter_number
templates.env.filters["ts"] = _filter_timestamp


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("Starting IB polling service (interval=%ds)", service.poll_interval)
    service.start()


@app.on_event("shutdown")
async def shutdown():
    service.stop()


# ── HTML endpoints ────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/stream-html")
async def stream_html():
    """
    SSE stream that pushes rendered HTML fragments on each new snapshot.
    HTMX listens on this endpoint and swaps the fragment into the page with
    no JavaScript required on the client side.
    """
    async def event_generator():
        last_as_of = 0.0
        while True:
            try:
                snapshot = service.get_snapshot()
                if snapshot is not None:
                    as_of = snapshot.get("as_of", 0.0)
                    if as_of > last_as_of:
                        last_as_of = as_of
                        html = templates.get_template("_fragment.html").render(snapshot=snapshot)
                        # SSE multi-line: each line of the HTML gets its own "data: " prefix.
                        # The browser's EventSource joins them back with newlines per spec.
                        lines = html.split("\n")
                        data = "\n".join(f"data: {line}" for line in lines)
                        yield f"{data}\n\n"
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("stream-html error: %s", e)
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── JSON endpoints ────────────────────────────────────────────────────────────

@app.get("/snapshot")
async def snapshot():
    """Current portfolio snapshot — positions, metrics, and health."""
    data = service.get_snapshot()
    if data is None:
        return JSONResponse(
            {"error": "Initial fetch in progress — try again in a few seconds"},
            status_code=503,
        )
    return data


@app.get("/health")
async def health():
    """IB connection and quote-quality diagnostics."""
    h = service.get_health()
    if h is None:
        return JSONResponse({"error": "No health data yet"}, status_code=503)
    return h


@app.get("/stream")
async def stream():
    """SSE stream of raw JSON snapshots (for non-HTML clients)."""
    async def event_generator():
        last_as_of = 0.0
        while True:
            snapshot = service.get_snapshot()
            if snapshot is not None:
                as_of = snapshot.get("as_of", 0.0)
                if as_of > last_as_of:
                    last_as_of = as_of
                    yield f"data: {json.dumps(snapshot)}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
