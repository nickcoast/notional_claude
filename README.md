# Notional Dashboard

Portfolio dashboard for Interactive Brokers, built on FastAPI + HTMX.
Connects to TWS running locally; no separate auth is needed — the TWS session is the auth gate.

## Starting the app

```sh
./run.sh
```

Then open http://127.0.0.1:8000 in a browser. TWS must already be running and accepting API connections on port 7497 (paper) or 7496 (live).

The equivalent manual command: `uvicorn api:app --host 127.0.0.1 --port 8000`

To view the dashboard from another device on the same trusted network, start it
on all network interfaces:

```sh
./run.sh --lan
```

The script prints a same-network URL such as `http://192.168.1.23:8000` for
your phone. macOS may ask whether to allow incoming connections for Python; allow
that for the same-network URL to work. Keep this LAN mode on trusted networks
only: the app does not have separate login/auth beyond access to the running Mac
and TWS session.

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
| `GET /news/{symbol}.json` | Recent IBKR API news headlines for a symbol |
| `GET /news/article.json` | IBKR API news article body by provider/article id |
| `GET /history.json` | Stored Net Liquidation history points (JSON) |
| `GET /history/compare` | Position value changes between two stored snapshots |
| `GET /health` | IB connection and quote-quality diagnostics |
| `GET /accounts` | Managed accounts with display names |
| `POST /account` | Set active account filter |
| `POST /account/name` | Set or clear an account nickname |
| `GET /debug/positions` | Per-contract position detail for verifying calculations |
| `GET /stream` | SSE stream of raw JSON snapshots |

The orders page is display-only and does not submit, modify, or cancel trades.
It reads open orders from `ib_insync` using local `openTrades` plus
`reqAllOpenOrders` by default. The app never calls `placeOrder`, `cancelOrder`,
or `reqGlobalCancel`. `IB_OPEN_ORDER_SCOPE=client` uses `reqOpenOrders`, which
TWS may reject when API Read-Only mode is enabled because that request can bind
manual TWS orders when used by client id `0`. This app uses a nonzero client id,
but `reqAllOpenOrders` is still the better default for a read-only dashboard
that needs to display orders placed outside this app.

Open-order rows include a calculated `Away` percentage: the absolute distance
between the actionable order price and current market reference price. Stock
orders use last trade when available; option orders prefer the bid/ask midpoint.
The orders page reconciles open-order status with same-session executions so a
stale open-order status does not keep a filled order looking actionable. Filled
orders are hidden by default and can be shown with the `Show filled` toggle.
Each poll stores the displayed order rows in SQLite and de-duplicates execution
fills by IB execution id. On startup, the service asks TWS for recent executions
using a read-only `reqExecutions` request so completed fills can survive app
restarts. The default backfill window is 7 days, but IBKR only returns what TWS
has available in its Trade Log settings; IB Gateway is typically limited to the
current day. Older stored executions remain available for future history views,
but the live Orders page only synthesizes filled rows for today's executions so
`Show filled` does not turn into a multi-day trade log.

Portfolio and order symbols use the same earnings-date highlighting: red within
3 days, orange within 7 days, and amber within 30 days. Tapping a highlighted
symbol opens an in-app details modal instead of relying on browser tooltips or
classic JavaScript dialogs. Earnings lookups are cached in SQLite as well as in
memory, so restarting the app can reuse recent lookup results. Previously seen
earnings dates are retained after they pass, because past earnings dates may be
useful for later chart annotations and post-mortems.

Portfolio and order symbols also include an `N` news button. Clicking it opens a
symbol news drawer that lazily fetches recent IBKR API headlines, shows the
headline count on the button, and fetches/caches the full article body only when
a headline is selected. IBKR separates headline requests from article-body
requests: headlines return provider code plus article id, and
`reqNewsArticle` uses that pair to return the article body. Article type `0`
is text or HTML; article type `1` is binary/PDF content encoded as text. API
news requires IBKR API news entitlements, which may differ from TWS news
subscriptions. Some provider headlines include metadata prefixes such as
`{A:800015:L:en}`; the app keeps the raw headline in JSON but strips the prefix
from the UI, displays the language code separately, converts headline timestamps
to browser-local time, and marks near-duplicate headlines as similar. IBKR
exposes a provider/article id for each article, but not a separate canonical
"story update" id.

The history page persists one SQLite row per successful poll. Restarting the app
reloads the existing `history.sqlite3` data and appends new snapshots. Account
snapshots store Net Liquidation and other account-level metrics for charting.
History defaults to the current calendar day and has its own account selector,
using account nicknames from `config.json`. The SQLite schema stores
`account_filter` on account, symbol, contract, and daily-extreme rows, so it can
hold history for multiple accounts; the polling service writes the account
filter active for that poll. Symbol-level position snapshots store actual market
value (`stock_value + option_actual_value`), stock value, option actual value,
option notional value, and NPV. The default History comparison uses actual
value, because option notional is an exposure metric and does not directly
explain NLV changes.

History comparisons estimate each position's contribution to the selected NLV
move. When quantity changes and a cost basis is available, the comparison
subtracts estimated trade flow from raw market-value change so adding or
reducing shares does not dominate the list purely because capital moved into or
out of the position. When execution fills are stored for the selected interval,
the comparison uses actual signed execution flow instead of cost-basis
estimates. Contract-level portfolio marks are also stored so later views can
drill into individual stock or option contracts, and same-day unavailable option
marks fall back to the most recent reliable mark for that contract. After the
same-day exercise cutoff, expiring options are valued at intrinsic value instead
of stale option-market marks. An `Other` reconciliation row accounts for cash
movement, fees, omitted rows, timing differences, and other non-position deltas
so displayed contributions add back to the selected NLV change. Daily high/low
Net Liquidation values are rolled up as snapshots arrive.

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
- Keep live and historical data paths split deliberately. The polling service
  should continue to write fresh IB results to SQLite and update an in-memory
  snapshot for the live Portfolio and Orders pages. Historical views, future
  order-history views, and analytics should read from SQLite. This keeps live
  SSE refreshes fast while making the database the durable source for charts,
  completed fills, earnings annotations, and post-mortems.

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
| `IB_READONLY` | `1` | Tell `ib_insync` to avoid its startup order-sync requests; this is separate from TWS API Read-Only mode |
| `IB_OPEN_ORDER_SCOPE` | `all` | Open-order query scope: `local`, `all`, or `client`; `all` uses `reqAllOpenOrders`, while `client` uses `reqOpenOrders` and may be rejected by TWS API Read-Only mode |
| `IB_EXECUTION_BACKFILL_DAYS` | `7` | Days of executions to request from TWS at startup, capped at 7 and limited by TWS/Gateway availability |
| `IB_LOG_FILE` | `ib_service.log` | Service log file path |
| `IB_LOG_MAX_BYTES` | `5242880` | Bytes per service log file before rotation |
| `IB_LOG_BACKUP_COUNT` | `5` | Number of rotated service log files to keep |
| `IB_NEWS_PROVIDERS` | auto | `+`-separated IBKR API news provider codes; defaults to subscribed providers returned by TWS |
| `IB_NEWS_CACHE_TTL` | `300` | Seconds to cache headline and article-body responses |
| `IB_NEWS_KEYWORDS` | empty | Comma-separated keywords to tag in news headlines and article bodies |
