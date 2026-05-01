# Notional Dashboard

Portfolio dashboard for Interactive Brokers, built on FastAPI + HTMX.
Connects to TWS running locally; no separate auth is needed — the TWS session is the auth gate.

## Starting the app

```sh
./run.sh
```

Then open http://127.0.0.1:8000 in a browser. TWS must already be running and accepting API connections on port 7497 (paper) or 7496 (live).

The equivalent manual command: `uvicorn api:app --host 127.0.0.1 --port 8000`

## Configuration

Edit `config.json` to set account nicknames (created automatically on first run):

```json
{
  "account_names": {
    "U1234567": "YOLO",
    "U7654321": "OG"
  },
  "selected_account": "ALL"
}
```

The active account can also be changed from the dropdown in the dashboard header.
The selection is saved back to `config.json` automatically.

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard UI |
| `GET /orders` | Read-only open orders UI |
| `GET /snapshot` | Current portfolio snapshot (JSON) |
| `GET /orders.json` | Current open orders snapshot (JSON) |
| `GET /health` | IB connection and quote-quality diagnostics |
| `GET /accounts` | Managed accounts with display names |
| `POST /account` | Set active account filter |
| `POST /account/name` | Set or clear an account nickname |
| `GET /debug/positions` | Per-contract position detail for verifying calculations |
| `GET /stream` | SSE stream of raw JSON snapshots |

The orders page is display-only. It reads open orders from `ib_insync`
(`reqAllOpenOrders`, `reqOpenOrders`, and local `openTrades`) and does not
submit, modify, or cancel trades.

Open-order rows include a calculated `Away` percentage: the absolute distance
between the actionable order price and current market reference price. Stock
orders use last trade when available; option orders prefer the bid/ask midpoint.

Portfolio and order symbols use the same earnings-date highlighting: red within
3 days, orange within 7 days, and amber within 30 days.

## Future work

- Flag open-order quantity exposure that exceeds the current position. For each
  symbol, compare open orders on either side of the market against the held
  position so the app can warn when pending close/reduction orders, including
  OCA groups, may add up to more than the position after partial exits or
  forgotten order adjustments.
- Save time-series history for post-mortems on large account-value moves. A
  first pass could persist daily high/low Net Liquidation values. A fuller
  implementation should capture account-level values and per-position marks so
  large NLV spikes and drops can be explained later by symbol and contract. For
  pricing, use last trade for stocks and investigate whether IB's own option
  valuation is closest to bid/ask midpoint, model price, or another mark before
  choosing the stored option reference price.
- Add configurable table columns. Portfolio and order tables should support
  drag-to-reorder columns plus a column-visibility menu, likely behind a gear
  icon with checkboxes. Hidden columns should still be fetched and kept current
  so they reappear immediately and remain available for analytics such as
  time-series storage.

## What is calculated vs. from IB

**From IB directly** (`accountSummary`):
- Net Liquidation, Gross Position Value, Buying Power

**Calculated by the app:**
- **NGAV** (Notional Gross Asset Value) — per symbol: `stock_value + (Opt δ Shares × underlying_price)`. Options are counted at their delta-equivalent notional, not their market price.
- **Notional Leverage Ratio** — `NGAV ÷ Net Liquidation`
- **Standard Leverage Ratio** — `Gross Position Value ÷ Net Liquidation`

## Persistence

| File | Contents |
|---|---|
| `config.json` | Account nicknames, selected account |
| `delta_cache.json` | Last-known option deltas (survives market close) |
| `price_cache.json` | Last-known underlying prices (reduces cost-basis fallback) |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `IB_POLL_INTERVAL` | `15` | Seconds between portfolio fetches (minimum 10) |
