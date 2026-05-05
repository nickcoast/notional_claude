"""
FastAPI backend for the IB Portfolio dashboard.

Run with:
    uvicorn api:app --host 127.0.0.1 --port 8000

Configuration via environment variables:
    IB_POLL_INTERVAL   Seconds between portfolio fetches (default 15, min 10)
    IB_HISTORY_DB      SQLite path for stored history (default history.sqlite3)
    IB_NEWS_PROVIDERS  Optional + separated IBKR API news provider codes
    IB_NEWS_KEYWORDS   Optional comma-separated news keyword tags
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
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

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="6" fill="#0f172a"/>
<path d="M7 22h18M9 18l5-5 4 4 5-7" fill="none" stroke="#38bdf8" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""


# ── Jinja2 templates ──────────────────────────────────────────────────────────

templates = Jinja2Templates(directory="templates")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    """Small inline favicon to avoid noisy browser 404s."""
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


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


@app.get("/orders")
async def orders_page(request: Request):
    return templates.TemplateResponse("orders.html", {"request": request})


@app.get("/history")
async def history_page(request: Request):
    return templates.TemplateResponse("history.html", {"request": request})


@app.get("/debug/positions", response_class=HTMLResponse)
async def debug_positions():
    """Per-contract position detail for verifying delta and notional calculations."""
    d = service.get_debug()
    if d is None:
        return HTMLResponse("<p>No data yet — waiting for first poll cycle.</p>", status_code=503)

    ts = datetime.fromtimestamp(d["as_of"]).strftime("%Y-%m-%d %H:%M:%S")
    account = d["account"]
    rows = []
    for sym, info in d["symbols"].items():
        price = info["underlying_price"]
        price_str = f"${price:,.2f}" if price is not None else "—"
        source = info["price_source"] or "—"
        stock_qty = info["stock_qty"]
        options = info["options"]
        total_ds = info["total_delta_shares"]
        total_nv = info["total_notional_value"]

        rows.append(f"""
<tr class="sym-row">
  <td colspan="7" style="padding:8px 4px 2px;font-weight:bold;font-size:1.05em;border-top:1px solid #444">
    {sym}
    <span style="font-weight:normal;color:#aaa;font-size:.9em">
      &nbsp;·&nbsp;price: {price_str} ({source})
      &nbsp;·&nbsp;shares: {stock_qty:,.0f}
    </span>
  </td>
</tr>""")
        if options:
            rows.append("""
<tr style="color:#888;font-size:.85em">
  <td style="padding:2px 4px">Expiry</td>
  <td style="padding:2px 4px;text-align:right">Strike</td>
  <td style="padding:2px 4px;text-align:center">P/C</td>
  <td style="padding:2px 4px;text-align:right">Qty</td>
  <td style="padding:2px 4px;text-align:right">Delta</td>
  <td style="padding:2px 4px;text-align:right">Mult</td>
  <td style="padding:2px 4px;text-align:right">δ Shares (qty × delta × mult)</td>
</tr>""")
            for c in options:
                ds_color = "#f87171" if c["delta_shares"] < 0 else "#d1d5db"
                rows.append(f"""
<tr>
  <td style="padding:2px 4px;color:#d1d5db">{c['expiry']}</td>
  <td style="padding:2px 4px;text-align:right;color:#d1d5db">{c['strike']:,.2f}</td>
  <td style="padding:2px 4px;text-align:center;color:#d1d5db">{c['right']}</td>
  <td style="padding:2px 4px;text-align:right;color:#d1d5db">{c['qty']:+.0f}</td>
  <td style="padding:2px 4px;text-align:right;color:#d1d5db">{c['delta']:+.5f}</td>
  <td style="padding:2px 4px;text-align:right;color:#d1d5db">{int(c['multiplier'])}</td>
  <td style="padding:2px 4px;text-align:right;color:{ds_color}">{c['delta_shares']:+.2f}</td>
</tr>""")
            nv_str = f"${total_nv:,.2f}" if total_nv is not None else "—"
            nv_color = "#f87171" if total_nv is not None and total_nv < 0 else "#34d399"
            rows.append(f"""
<tr style="border-top:1px solid #333">
  <td colspan="6" style="padding:3px 4px;text-align:right;color:#888;font-size:.9em">Total δ Shares → Notional Value</td>
  <td style="padding:3px 4px;text-align:right;font-weight:bold;color:{nv_color}">{total_ds:+.2f} → {nv_str}</td>
</tr>""")
        else:
            rows.append("""<tr><td colspan="7" style="padding:2px 4px;color:#666;font-size:.9em">no option positions</td></tr>""")

    body = "\n".join(rows)
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Position Debug</title>
  <style>
    body {{ background:#111; color:#e5e7eb; font-family:ui-monospace,'Cascadia Code',monospace; font-size:13px; padding:24px; }}
    table {{ border-collapse:collapse; width:100%; }}
    a {{ color:#60a5fa; }}
  </style>
</head>
<body>
  <p style="color:#6b7280">
    <a href="/">← back</a>
    &nbsp;·&nbsp; Account: <strong>{account}</strong>
    &nbsp;·&nbsp; As of: {ts}
  </p>
  <table>{body}</table>
</body>
</html>"""
    return HTMLResponse(html)


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


@app.get("/stream-orders-html")
async def stream_orders_html():
    """SSE stream that pushes rendered open-order table fragments."""
    async def event_generator():
        last_as_of = 0.0
        while True:
            try:
                orders = service.get_orders()
                if orders is not None:
                    as_of = orders.get("as_of", 0.0)
                    if as_of > last_as_of:
                        last_as_of = as_of
                        html = templates.get_template("_orders_fragment.html").render(snapshot=orders)
                        lines = html.split("\n")
                        data = "\n".join(f"data: {line}" for line in lines)
                        yield f"{data}\n\n"
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("stream-orders-html error: %s", e)
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


@app.get("/orders.json")
async def orders_json():
    """Current read-only open orders snapshot."""
    data = service.get_orders()
    if data is None:
        return JSONResponse(
            {"error": "Initial fetch in progress — try again in a few seconds"},
            status_code=503,
        )
    return data


@app.get("/news/article.json")
async def news_article_json(
    provider_code: str = Query(...),
    article_id: str = Query(...),
):
    """IBKR API news article body for a provider/article id pair."""
    try:
        return service.get_news_article(provider_code, article_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except TimeoutError as exc:
        return JSONResponse({"error": str(exc)}, status_code=504)
    except Exception as exc:
        logger.warning("news article request failed for %s/%s: %s", provider_code, article_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/news/{symbol}.json")
async def symbol_news_json(
    symbol: str,
    limit: int = Query(20, ge=1, le=100),
):
    """Recent IBKR API news headlines for a stock symbol."""
    try:
        return service.get_symbol_news(symbol, limit=limit)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except TimeoutError as exc:
        return JSONResponse({"error": str(exc)}, status_code=504)
    except Exception as exc:
        logger.warning("news headline request failed for %s: %s", symbol, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/history.json")
async def history_json(
    account: Optional[str] = None,
    limit: int = Query(1000, ge=1, le=10000),
    start_as_of: Optional[float] = None,
    end_as_of: Optional[float] = None,
):
    """Net Liquidation history points for charting."""
    return service.get_history(
        account_filter=account,
        limit=limit,
        start_as_of=start_as_of,
        end_as_of=end_as_of,
    )


@app.get("/history/compare")
async def history_compare(
    start_id: int = Query(..., ge=1),
    end_id: int = Query(..., ge=1),
    account: Optional[str] = None,
    basis: str = Query("market_value"),
    level: str = Query("symbol"),
    limit: int = Query(100, ge=1, le=500),
):
    """Compare position-value changes between two stored snapshots."""
    try:
        data = service.compare_history_positions(
            start_id=start_id,
            end_id=end_id,
            account_filter=account,
            basis=basis,
            level=level,
            limit=limit,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if data is None:
        return JSONResponse({"error": "Snapshot not found"}, status_code=404)
    return data


@app.get("/accounts")
async def accounts():
    """List of managed IB accounts with display names and current selection."""
    info = service.get_accounts()
    managed = info["managed"]
    names = info["names"]
    result = [{"id": "ALL", "name": "ALL"}]
    for acc_id in managed:
        result.append({"id": acc_id, "name": names.get(acc_id, acc_id)})
    return {"accounts": result, "selected": info["selected"]}


@app.post("/account")
async def set_account(request: Request):
    """Set the active account filter. Persisted to config.json."""
    body = await request.json()
    account = body.get("account", "ALL")
    valid = {"ALL"} | set(service.get_accounts()["managed"])
    if account not in valid:
        return JSONResponse({"error": f"Unknown account: {account}"}, status_code=400)
    service.set_account(account)
    return {"selected": account}


@app.post("/account/name")
async def set_account_name(request: Request):
    """Set or clear a display name for an account. Persisted to config.json."""
    body = await request.json()
    account = body.get("account", "").strip()
    name = body.get("name", "").strip()
    if not account or account == "ALL":
        return JSONResponse({"error": "Invalid account"}, status_code=400)
    valid = set(service.get_accounts()["managed"])
    if account not in valid:
        return JSONResponse({"error": f"Unknown account: {account}"}, status_code=400)
    service.set_account_name(account, name)
    return {"account": account, "name": name or account}


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
