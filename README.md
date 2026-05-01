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
| `GET /history` | Net Liquidation history and position-change UI |
| `GET /snapshot` | Current portfolio snapshot (JSON) |
| `GET /orders.json` | Current open orders snapshot (JSON) |
| `GET /history.json` | Stored Net Liquidation history points (JSON) |
| `GET /history/compare` | Position value changes between two stored snapshots |
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

The history page persists one SQLite row per successful poll. Restarting the app
reloads the existing `history.sqlite3` data and appends new snapshots. Account
snapshots store Net Liquidation and other account-level metrics for charting.
Symbol-level position snapshots store actual market value (`stock_value +
option_actual_value`), stock value, option actual value, option notional value,
and NPV. The default History comparison uses actual value, because option
notional is an exposure metric and does not directly explain NLV changes.

History comparisons estimate each position's contribution to the selected NLV
move. When quantity changes and a cost basis is available, the comparison
subtracts estimated trade flow from raw market-value change so adding or
reducing shares does not dominate the list purely because capital moved into or
out of the position. Contract-level portfolio marks are also stored so later
views can drill into individual stock or option contracts. Daily high/low Net
Liquidation values are rolled up as snapshots arrive.

## Future work

- Flag open-order quantity exposure that exceeds the current position. For each
  symbol, compare open orders on either side of the market against the held
  position so the app can warn when pending close/reduction orders, including
  OCA groups, may add up to more than the position after partial exits or
  forgotten order adjustments.
- Investigate whether IB's option portfolio marks are closest to bid/ask
  midpoint, model price, or another mark before adding alternate option-price
  bases to history comparisons.
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
| `history.sqlite3` | Account, symbol, contract, and daily high/low history |

## Testing

Run the standard-library test suite with:

```sh
python3 -m unittest discover -s tests
```

The time-series tests include a small anonymized fixture exported from a live
`history.sqlite3` database. Account IDs, symbols, raw JSON payloads, and full
database contents are not committed.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `IB_POLL_INTERVAL` | `15` | Seconds between portfolio fetches (minimum 10) |
| `IB_HISTORY_DB` | `history.sqlite3` | SQLite path for stored time-series history |
