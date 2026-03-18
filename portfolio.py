import logging
import time
import traceback
from collections import deque

import pandas as pd

from market_data import (
    contract_multiplier,
    gather_option_market_data,
    gather_stock_market_data,
    gather_stock_snapshot_data,
    option_contract_key,
    option_delta_bs_fallback,
    option_delta_from_ticker,
    option_underlying_price_from_ticker,
    pick_price_from_ticker,
)
from utils import (
    format_currency,
    get_account_value,
    is_valid_number,
    safe_float_conversion,
    time_operation,
)
from ib_insync import Option, Stock

logger = logging.getLogger(__name__)

PREFERRED_MARKET_DATA_TYPE = 3
CACHEABLE_UNDERLYING_SOURCES = {
    "portfolio",
    "portfolio_derived",
    "portfolio_value",
    "option_greeks",
    "snapshot",
    "snapshot_retry",
    "stream_retry",
}
HIGH_CONFIDENCE_UNDERLYING_SOURCES = {
    "portfolio",
    "portfolio_derived",
    "portfolio_value",
    "option_greeks",
}
RETRYABLE_UNDERLYING_SOURCES = {"unavailable", "snapshot", "snapshot_retry"}
QUOTE_RETRY_MAX_ATTEMPTS = 6
QUOTE_RETRY_COOLDOWN_SECONDS = 8
QUOTE_RETRY_BATCH_SIZE = 6
IB_CONNECTION_ERROR_CODES = {502, 504, 1100, 1101, 1102, 1300, 2110}
IB_FARM_WARNING_CODES = {2103, 2104, 2105, 2106, 2157, 2158}
IB_QUOTE_ERROR_CODES = {354}

# Process-lifetime state — survives Streamlit reruns because this module is
# imported once per process, not re-executed on each rerun like the main script.
_runtime_cache = {
    'underlying_prices': {},
    'option_deltas': {},
    'quote_retry_state': {},
}

_diagnostics_state = {
    'events': deque(maxlen=500),
    'farm_status': {},
    'registered': False,
}


def parse_ib_farm_from_error(error_message):
    """Extract farm identifier from IB warning strings like '...:usopt'."""
    if not isinstance(error_message, str):
        return None
    if ":" not in error_message:
        return None
    return error_message.rsplit(":", 1)[-1].strip() or None


def register_ib_error_handler(ib):
    """Register a single IB error-event handler for diagnostics."""
    if _diagnostics_state.get('registered'):
        return

    def handle_ib_error(req_id, error_code, error_message, contract):
        now_ts = time.time()
        symbol = getattr(contract, 'symbol', None) if contract else None
        _diagnostics_state['events'].append({
            'ts': now_ts,
            'req_id': req_id,
            'code': int(error_code),
            'message': error_message,
            'symbol': symbol,
        })

        if int(error_code) in IB_FARM_WARNING_CODES:
            farm = parse_ib_farm_from_error(error_message)
            if farm:
                _diagnostics_state['farm_status'][farm] = {
                    'code': int(error_code),
                    'message': error_message,
                    'ts': now_ts,
                }

    ib.errorEvent += handle_ib_error
    _diagnostics_state['registered'] = True


def get_recent_ib_events(since_ts):
    return [event for event in _diagnostics_state['events'] if event.get('ts', 0) >= since_ts]


def clear_quote_cache():
    """Clear all process-level quote caches."""
    _runtime_cache['underlying_prices'].clear()
    _runtime_cache['option_deltas'].clear()
    _runtime_cache['quote_retry_state'].clear()


@time_operation("Portfolio Data Retrieval")
def get_portfolio_data_sync(
    ib,
    *,
    option_delta_cache: dict,
    underlying_price_cache: dict,
    force_retry_now: bool = False,
    auto_retry_enabled: bool = False,
):
    """
    Fetch and aggregate portfolio data from IB.

    Caches are passed in as mutable dicts and updated in-place so the caller
    (e.g. Streamlit session_state, or a future service layer) owns the lifetime.

    Returns (account_df, underlying_df, positions_by_underlying, health_dict).
    health_dict is always returned, even on failure.
    """
    logger.info("Starting portfolio data retrieval")
    started_at = time.time()

    def _empty_health(**kwargs):
        base = {
            'as_of': time.time(),
            'connection_ok': False,
            'connection_issue_count': 0,
            'quote_issue_count': 0,
            'farm_down_count': 0,
            'farm_down_names': [],
            'quote_issue_symbols': [],
            'fallback_symbols': [],
        }
        base.update(kwargs)
        return base

    try:
        try:
            ib.reqMarketDataType(PREFERRED_MARKET_DATA_TYPE)
        except Exception as market_data_type_error:
            logger.warning(f"Unable to set market data type {PREFERRED_MARKET_DATA_TYPE}: {market_data_type_error}")

        logger.info("Fetching account data...")
        t0 = time.time()
        account_summary = ib.accountSummary()
        logger.debug(f"Fetched account data in {time.time() - t0:.2f}s")
        if not account_summary:
            logger.warning("Account summary is empty")
            return None, None, None, _empty_health(
                connection_issue_count=1,
                note='Empty account summary from IB API',
            )

        account_df = pd.DataFrame([(row.tag, row.value) for row in account_summary], columns=['Tag', 'Value'])
        account_df = account_df.set_index('Tag')

        logger.info("Fetching positions...")
        t0 = time.time()
        positions = ib.positions() or []
        logger.debug(f"Fetched positions in {time.time() - t0:.2f}s")
        if positions:
            logger.info(f"Got {len(positions)} positions")
        else:
            logger.warning("No positions found")

        t0 = time.time()
        portfolio_items = ib.portfolio() or []
        logger.debug(f"Fetched portfolio items in {time.time() - t0:.2f}s")

        positions_by_underlying = {}
        position_errors = 0

        portfolio_by_account_conid = {}
        portfolio_by_conid_any = {}
        portfolio_by_account_option_key = {}
        portfolio_by_option_key_any = {}
        underlying_market_price_map = {}
        underlying_price_source = {}

        persistent_underlying_price_cache = _runtime_cache['underlying_prices']
        persistent_option_delta_cache = _runtime_cache['option_deltas']
        quote_retry_state = _runtime_cache['quote_retry_state']

        cache_ttl_seconds = 180
        now_ts = time.time()

        def unpack_numeric_cache(raw_cache):
            """Read numeric cache entries, honoring TTL for timestamped payloads."""
            unpacked = {}
            for key, payload in raw_cache.items():
                if isinstance(payload, dict):
                    value = payload.get('value')
                    timestamp = safe_float_conversion(payload.get('ts'))
                    if now_ts - timestamp <= cache_ttl_seconds and is_valid_number(value):
                        unpacked[key] = float(value)
                elif is_valid_number(payload):
                    # Backward compatibility for older cache payload shape.
                    unpacked[key] = float(payload)
            return unpacked

        # Prune stale entries from process-level caches.
        for key, payload in list(persistent_underlying_price_cache.items()):
            if isinstance(payload, dict):
                if now_ts - safe_float_conversion(payload.get('ts')) > cache_ttl_seconds:
                    del persistent_underlying_price_cache[key]
        for key, payload in list(persistent_option_delta_cache.items()):
            if isinstance(payload, dict):
                if now_ts - safe_float_conversion(payload.get('ts')) > cache_ttl_seconds:
                    del persistent_option_delta_cache[key]

        for item in portfolio_items:
            contract = item.contract
            account = getattr(item, 'account', '')
            con_id = getattr(contract, 'conId', 0)
            if con_id:
                portfolio_by_account_conid[(account, con_id)] = item
                portfolio_by_conid_any.setdefault(con_id, item)

            if contract.secType == 'OPT':
                option_key = option_contract_key(contract)
                portfolio_by_account_option_key[(account, option_key)] = item
                portfolio_by_option_key_any.setdefault(option_key, item)
            elif contract.secType == 'STK':
                item_price = safe_float_conversion(getattr(item, 'marketPrice', None))
                if not (item_price > 0):
                    market_value = safe_float_conversion(getattr(item, 'marketValue', None))
                    position_qty = safe_float_conversion(getattr(item, 'position', None))
                    if position_qty != 0 and market_value != 0:
                        item_price = abs(market_value / position_qty)
                        if item_price > 0:
                            underlying_price_source[contract.symbol] = "portfolio_derived"
                if item_price > 0:
                    underlying_market_price_map[contract.symbol] = item_price
                    if contract.symbol not in underlying_price_source:
                        underlying_price_source[contract.symbol] = "portfolio"
                    persistent_underlying_price_cache[contract.symbol] = {
                        'value': float(item_price),
                        'ts': now_ts,
                    }

        # Persist last-known values so brief quote gaps do not regress to cost basis.
        delta_cache = unpack_numeric_cache(persistent_option_delta_cache)
        delta_cache.update(option_delta_cache)
        merged_price_cache = unpack_numeric_cache(persistent_underlying_price_cache)
        merged_price_cache.update(underlying_price_cache)

        def cache_option_delta(key, delta_value):
            if not is_valid_number(delta_value):
                return
            numeric_delta = float(delta_value)
            delta_cache[key] = numeric_delta
            option_delta_cache[key] = numeric_delta
            persistent_option_delta_cache[key] = {
                'value': numeric_delta,
                'ts': time.time(),
            }

        def cache_underlying_price(symbol, price_value, source=None):
            if source not in CACHEABLE_UNDERLYING_SOURCES:
                return
            if not is_valid_number(price_value) or float(price_value) <= 0:
                return
            numeric_price = float(price_value)
            merged_price_cache[symbol] = numeric_price
            underlying_price_cache[symbol] = numeric_price
            persistent_underlying_price_cache[symbol] = {
                'value': numeric_price,
                'ts': time.time(),
            }

        option_contracts = []
        seen_option_keys = set()
        underlying_symbols = sorted({pos.contract.symbol for pos in positions if pos.contract.symbol})
        for symbol in list(quote_retry_state.keys()):
            if symbol not in underlying_symbols:
                del quote_retry_state[symbol]
        for pos in positions:
            contract = pos.contract
            if contract.secType != 'OPT':
                continue
            key = option_contract_key(contract)
            if key in seen_option_keys:
                continue
            seen_option_keys.add(key)
            option_contract = Option(
                contract.symbol,
                contract.lastTradeDateOrContractMonth,
                float(contract.strike),
                contract.right,
                contract.exchange or 'SMART',
                contract.multiplier or '100',
                contract.currency or 'USD',
                tradingClass=contract.tradingClass or contract.symbol,
            )
            if getattr(contract, 'conId', 0):
                option_contract.conId = contract.conId
            option_contracts.append(option_contract)

        # Pull option deltas/quotes and feed underlying prices from option greeks.
        option_delta_map = {}
        option_price_map = {}
        option_keys_with_delta = set()
        option_keys_with_und_price = set()
        option_keys_with_price = set()

        def absorb_option_tickers(option_tickers):
            for key, ticker in option_tickers.items():
                delta = option_delta_from_ticker(ticker)
                if not is_valid_number(delta):
                    # IB delayed/frozen data often provides undPrice + impliedVol
                    # but leaves delta as NaN.  Fall back to Black-Scholes.
                    _sym, expiry_str, strike, right = key[0], key[1], key[2], key[3]
                    delta = option_delta_bs_fallback(ticker, strike, expiry_str, right)
                if is_valid_number(delta):
                    option_delta_map[key] = float(delta)
                    cache_option_delta(key, delta)
                    option_keys_with_delta.add(key)

                option_price = pick_price_from_ticker(ticker)
                if option_price is not None:
                    option_price_map[key] = option_price
                    option_keys_with_price.add(key)

                underlying_symbol = key[0]
                und_price = option_underlying_price_from_ticker(ticker)
                if und_price is not None:
                    option_keys_with_und_price.add(key)
                    if underlying_symbol not in underlying_market_price_map:
                        underlying_market_price_map[underlying_symbol] = und_price
                        underlying_price_source[underlying_symbol] = "option_greeks"
                        cache_underlying_price(underlying_symbol, und_price, source="option_greeks")

        def fetch_underlyings(symbols, wait_seconds, chunk_size, label, source_tag, only_missing=True):
            target_symbols = [s for s in symbols if s]
            if only_missing:
                target_symbols = [s for s in target_symbols if s not in underlying_market_price_map]
            if not target_symbols:
                return

            t0 = time.time()
            stock_contracts = [Stock(symbol, 'SMART', 'USD') for symbol in target_symbols]
            stock_tickers = gather_stock_market_data(
                ib,
                stock_contracts,
                wait_seconds=wait_seconds,
                chunk_size=chunk_size,
            )
            logger.debug(
                f"{label}: requested {len(stock_contracts)} underlying streams in {time.time() - t0:.2f}s"
            )

            stream_resolved = set()
            for symbol, ticker in stock_tickers.items():
                price = pick_price_from_ticker(ticker)
                if price is not None:
                    existing_source = underlying_price_source.get(symbol)
                    if existing_source in HIGH_CONFIDENCE_UNDERLYING_SOURCES:
                        stream_resolved.add(symbol)
                        continue
                    underlying_market_price_map[symbol] = price
                    underlying_price_source[symbol] = source_tag
                    cache_underlying_price(symbol, price, source=source_tag)
                    stream_resolved.add(symbol)

            if only_missing:
                snapshot_targets = [s for s in target_symbols if s not in underlying_market_price_map]
            else:
                snapshot_targets = [s for s in target_symbols if s not in stream_resolved]

            if snapshot_targets:
                t0 = time.time()
                snapshot_contracts = [Stock(symbol, 'SMART', 'USD') for symbol in snapshot_targets]
                snapshot_tickers = gather_stock_snapshot_data(
                    ib,
                    snapshot_contracts,
                    timeout_seconds=max(2.0, wait_seconds * 3),
                    chunk_size=max(4, min(chunk_size, 8)),
                )
                logger.debug(
                    f"{label}: requested {len(snapshot_contracts)} underlying snapshots in {time.time() - t0:.2f}s"
                )
                for symbol, ticker in snapshot_tickers.items():
                    price = pick_price_from_ticker(ticker)
                    if price is not None:
                        existing_source = underlying_price_source.get(symbol)
                        if existing_source in HIGH_CONFIDENCE_UNDERLYING_SOURCES:
                            continue
                        underlying_market_price_map[symbol] = price
                        underlying_price_source[symbol] = source_tag
                        cache_underlying_price(symbol, price, source=source_tag)

            unresolved_symbols = [s for s in target_symbols if s not in underlying_market_price_map]
            if unresolved_symbols:
                logger.info(f"{label}: unresolved underlyings after fetch: {unresolved_symbols}")

        due_retry_symbols = []
        for symbol, state in quote_retry_state.items():
            attempts = int(safe_float_conversion(state.get('attempts')))
            next_retry_ts = safe_float_conversion(state.get('next_retry_ts'))
            if attempts >= QUOTE_RETRY_MAX_ATTEMPTS:
                continue
            if force_retry_now or (auto_retry_enabled and now_ts >= next_retry_ts):
                due_retry_symbols.append((attempts, next_retry_ts, symbol))
        due_retry_symbols.sort(key=lambda item: (item[0], item[1], item[2]))
        queued_retry_symbols = [symbol for _, _, symbol in due_retry_symbols[:QUOTE_RETRY_BATCH_SIZE]]

        if option_contracts:
            t0 = time.time()
            option_tickers = gather_option_market_data(
                ib,
                option_contracts,
                wait_seconds=0.6,
                chunk_size=6,
            )
            logger.debug(
                f"Requested {len(option_contracts)} option streams in {time.time() - t0:.2f}s"
            )
            absorb_option_tickers(option_tickers)

        fetch_underlyings(
            underlying_symbols,
            wait_seconds=0.5,
            chunk_size=12,
            label="Initial fetch",
            source_tag="snapshot",
            only_missing=True,
        )

        # If live coverage is poor, do a slower second pass before relying on cache/fallback.
        min_live_quotes = max(5, int(len(underlying_symbols) * 0.5))
        if underlying_symbols and len(underlying_market_price_map) < min_live_quotes:
            logger.warning(
                f"Low live quote coverage ({len(underlying_market_price_map)}/{len(underlying_symbols)}); retrying fetch"
            )
            if option_contracts:
                t0 = time.time()
                option_tickers = gather_option_market_data(
                    ib,
                    option_contracts,
                    wait_seconds=1.0,
                    chunk_size=4,
                )
                logger.debug(
                    f"Retry fetch: requested {len(option_contracts)} option streams in {time.time() - t0:.2f}s"
                )
                absorb_option_tickers(option_tickers)

            fetch_underlyings(
                underlying_symbols,
                wait_seconds=1.0,
                chunk_size=8,
                label="Retry fetch",
                source_tag="snapshot",
                only_missing=True,
            )

        # Final slow pass for any symbols still unresolved after both normal passes.
        # Uses streaming-only with a longer wait — avoids reqTickers which times out
        # for certain symbols when IB's delayed data server is slow (common after hours).
        still_unresolved = [s for s in underlying_symbols if s not in underlying_market_price_map]
        if still_unresolved:
            logger.info("Slow-streaming retry for %d persistently unresolved symbols: %s",
                        len(still_unresolved), still_unresolved)
            slow_contracts = [Stock(s, 'SMART', 'USD') for s in still_unresolved]
            slow_tickers = gather_stock_market_data(ib, slow_contracts, wait_seconds=4.0, chunk_size=4)
            for symbol, ticker in slow_tickers.items():
                price = pick_price_from_ticker(ticker)
                if price is not None:
                    underlying_market_price_map[symbol] = price
                    underlying_price_source[symbol] = "snapshot"
                    cache_underlying_price(symbol, price, source="snapshot")
            for contract in slow_contracts:
                try:
                    ib.cancelMktData(contract)
                except Exception:
                    pass

        if queued_retry_symbols:
            queued_retry_symbol_set = set(queued_retry_symbols)
            retry_option_contracts = [c for c in option_contracts if c.symbol in queued_retry_symbol_set]
            if retry_option_contracts:
                t0 = time.time()
                option_tickers = gather_option_market_data(
                    ib,
                    retry_option_contracts,
                    wait_seconds=1.3,
                    chunk_size=3,
                )
                logger.debug(
                    f"Queued retry: requested {len(retry_option_contracts)} option streams in {time.time() - t0:.2f}s"
                )
                absorb_option_tickers(option_tickers)

            logger.info(f"Retry queue: refreshing underlying quotes for {queued_retry_symbols}")
            fetch_underlyings(
                queued_retry_symbols,
                wait_seconds=1.5,
                chunk_size=4,
                label="Queued retry",
                source_tag="snapshot_retry",
                only_missing=False,
            )

        if option_contracts:
            total_option_contracts = len(option_contracts)
            logger.info(
                "Option quote coverage: delta=%s/%s undPrice=%s/%s price=%s/%s",
                len(option_keys_with_delta),
                total_option_contracts,
                len(option_keys_with_und_price),
                total_option_contracts,
                len(option_keys_with_price),
                total_option_contracts,
            )

        for idx, pos in enumerate(positions, start=1):
            try:
                contract = pos.contract
                underlying_symbol = contract.symbol
                logger.debug(f"Processing position {idx}/{len(positions)}: {underlying_symbol}")

                if underlying_symbol not in positions_by_underlying:
                    positions_by_underlying[underlying_symbol] = {
                        'stock_count': 0.0,
                        'stock_value': 0.0,
                        'option_notional': 0.0,
                        'option_actual_value': 0.0,
                        'underlying_market_price': None,
                        'underlying_cost_basis_sum': 0.0,
                        'underlying_cost_basis_qty': 0.0,
                        'price_source': None,
                    }

                if contract.secType == 'STK':
                    positions_by_underlying[underlying_symbol]['stock_count'] += pos.position

                    con_id = getattr(contract, 'conId', 0)
                    account = getattr(pos, 'account', '')
                    portfolio_item = portfolio_by_account_conid.get((account, con_id))
                    if portfolio_item is None and con_id:
                        portfolio_item = portfolio_by_conid_any.get(con_id)
                    stock_market_value = None
                    if portfolio_item is not None and is_valid_number(getattr(portfolio_item, 'marketValue', None)):
                        stock_market_value = float(portfolio_item.marketValue)
                        if pos.position != 0:
                            implied_price = abs(stock_market_value / float(pos.position))
                            if implied_price > 0 and underlying_symbol not in underlying_market_price_map:
                                underlying_market_price_map[underlying_symbol] = implied_price
                                underlying_price_source[underlying_symbol] = "portfolio_value"
                                cache_underlying_price(underlying_symbol, implied_price, source="portfolio_value")

                    if stock_market_value is None:
                        known_market_price = underlying_market_price_map.get(underlying_symbol)
                        known_price_from_cache = False
                        if known_market_price is None:
                            known_market_price = merged_price_cache.get(underlying_symbol)
                            if known_market_price is not None:
                                underlying_price_source[underlying_symbol] = "cached"
                                known_price_from_cache = True
                        if is_valid_number(known_market_price) and float(known_market_price) > 0:
                            stock_market_value = float(known_market_price) * pos.position
                            underlying_market_price_map[underlying_symbol] = float(known_market_price)
                            if not known_price_from_cache:
                                cache_underlying_price(
                                    underlying_symbol,
                                    known_market_price,
                                    source=underlying_price_source.get(underlying_symbol),
                                )

                    if stock_market_value is None:
                        fallback_cost = safe_float_conversion(pos.avgCost)
                        stock_market_value = fallback_cost * pos.position
                        if fallback_cost > 0:
                            underlying_market_price_map.setdefault(underlying_symbol, fallback_cost)
                            underlying_price_source.setdefault(underlying_symbol, "cost_basis")

                    positions_by_underlying[underlying_symbol]['stock_value'] += stock_market_value

                    abs_qty = abs(float(pos.position))
                    avg_cost = safe_float_conversion(pos.avgCost)
                    if abs_qty > 0 and avg_cost > 0:
                        positions_by_underlying[underlying_symbol]['underlying_cost_basis_sum'] += abs_qty * avg_cost
                        positions_by_underlying[underlying_symbol]['underlying_cost_basis_qty'] += abs_qty

                elif contract.secType == 'OPT':
                    key = option_contract_key(contract)
                    multiplier = contract_multiplier(contract)

                    delta = option_delta_map.get(key)
                    if delta is None:
                        cached_delta = delta_cache.get(key)
                        if is_valid_number(cached_delta):
                            delta = float(cached_delta)

                    if delta is None:
                        # Prefer explicit "unknown delta" over synthetic deltas.
                        delta = 0.0

                    option_notional_shares = delta * multiplier * pos.position
                    positions_by_underlying[underlying_symbol]['option_notional'] += option_notional_shares

                    con_id = getattr(contract, 'conId', 0)
                    account = getattr(pos, 'account', '')
                    portfolio_item = portfolio_by_account_conid.get((account, con_id))
                    if portfolio_item is None and con_id:
                        portfolio_item = portfolio_by_conid_any.get(con_id)
                    if portfolio_item is None:
                        portfolio_item = portfolio_by_account_option_key.get((account, key))
                    if portfolio_item is None:
                        portfolio_item = portfolio_by_option_key_any.get(key)

                    option_actual_value = None
                    if portfolio_item is not None and is_valid_number(getattr(portfolio_item, 'marketValue', None)):
                        option_actual_value = float(portfolio_item.marketValue)

                    if option_actual_value is None:
                        option_price = option_price_map.get(key)
                        if option_price is None:
                            avg_cost_total = safe_float_conversion(pos.avgCost)
                            option_price = avg_cost_total / multiplier if multiplier else 0.0
                        option_actual_value = option_price * multiplier * pos.position

                    positions_by_underlying[underlying_symbol]['option_actual_value'] += option_actual_value
            except Exception as position_error:
                logger.warning(f"Error processing position {idx}: {position_error}")
                position_errors += 1
                continue

        if position_errors > 0:
            logger.warning(f"Encountered errors in {position_errors}/{len(positions)} positions")

        logger.info("Creating dataframe...")
        underlying_data = []
        total_npv = 0.0

        for symbol, data in positions_by_underlying.items():
            try:
                market_price = underlying_market_price_map.get(symbol)
                if not (is_valid_number(market_price) and float(market_price) > 0):
                    cached_price = merged_price_cache.get(symbol)
                    if is_valid_number(cached_price) and float(cached_price) > 0:
                        market_price = float(cached_price)
                        underlying_price_source[symbol] = "cached"

                if not (is_valid_number(market_price) and float(market_price) > 0):
                    if data['stock_count'] != 0 and is_valid_number(data['stock_value']):
                        derived_price = abs(float(data['stock_value']) / float(data['stock_count']))
                        if derived_price > 0:
                            market_price = derived_price
                            underlying_price_source[symbol] = "derived"

                if not (is_valid_number(market_price) and float(market_price) > 0):
                    qty = data['underlying_cost_basis_qty']
                    if qty > 0:
                        market_price = data['underlying_cost_basis_sum'] / qty
                        underlying_price_source[symbol] = "cost_basis"

                if not (is_valid_number(market_price) and float(market_price) > 0):
                    market_price = 0.0
                    underlying_price_source[symbol] = "unavailable"

                current_source = underlying_price_source.get(symbol)
                cache_underlying_price(symbol, market_price, source=current_source)
                cost_basis_price = 0.0
                if data['underlying_cost_basis_qty'] > 0:
                    cost_basis_price = data['underlying_cost_basis_sum'] / data['underlying_cost_basis_qty']

                option_notional_value = data['option_notional'] * market_price
                total_notional = data['stock_value'] + option_notional_value

                underlying_data.append({
                    'Symbol': symbol,
                    'Stock Count': data['stock_count'],
                    'Stock Value': data['stock_value'],
                    'Option Notional (Shares)': data['option_notional'],
                    'Option Notional Value': option_notional_value,
                    'Option Actual Value': data['option_actual_value'],
                    'Underlying Market Price': market_price,
                    'Underlying Cost Basis': cost_basis_price,
                    'Underlying Price Source': underlying_price_source.get(symbol, 'unknown'),
                    'Notional Position Value (NPV)': total_notional
                })
                total_npv += total_notional
            except Exception as calc_error:
                logger.warning(f"Error calculating values for {symbol}: {calc_error}")
                continue

        source_counts = {}
        for row in underlying_data:
            source = row.get('Underlying Price Source', 'unknown')
            source_counts[source] = source_counts.get(source, 0) + 1
        fallback_symbols = []
        if source_counts:
            logger.info(f"Underlying price sources: {source_counts}")
            fallback_symbols = [
                row.get('Symbol')
                for row in underlying_data
                if row.get('Underlying Price Source') in ('cost_basis', 'unavailable')
            ]
            if fallback_symbols:
                logger.info(f"Fallback-priced symbols: {fallback_symbols}")

        recent_ib_events = get_recent_ib_events(started_at - 2.0)
        connection_events = [e for e in recent_ib_events if e.get('code') in IB_CONNECTION_ERROR_CODES]
        quote_events = [e for e in recent_ib_events if e.get('code') in IB_QUOTE_ERROR_CODES]
        farm_down_names = sorted({
            parse_ib_farm_from_error(e.get('message'))
            for e in recent_ib_events
            if e.get('code') in {2103, 2105, 2157}
            and parse_ib_farm_from_error(e.get('message'))
        })
        quote_issue_symbols = sorted({
            e.get('symbol')
            for e in quote_events
            if e.get('symbol')
        })

        if connection_events:
            logger.warning(
                "IB diagnostics: connection-level issues detected in this cycle (%s events)",
                len(connection_events),
            )
        if quote_events:
            logger.warning(
                "IB diagnostics: quote entitlement/data issues detected (%s events, symbols=%s)",
                len(quote_events),
                quote_issue_symbols or "n/a",
            )
        if farm_down_names:
            logger.warning("IB diagnostics: farms reported degraded/down: %s", farm_down_names)

        retry_scheduled_symbols = []
        retry_exhausted_symbols = []
        retry_now_ts = time.time()
        for row in underlying_data:
            symbol = row.get('Symbol')
            source = row.get('Underlying Price Source', 'unknown')
            stock_count = safe_float_conversion(row.get('Stock Count'))
            option_notional_shares = safe_float_conversion(row.get('Option Notional (Shares)'))
            option_only_position = abs(stock_count) < 1e-9 and abs(option_notional_shares) > 0

            needs_retry = source == "unavailable" or (
                option_only_position and source in RETRYABLE_UNDERLYING_SOURCES
            )
            if not needs_retry:
                quote_retry_state.pop(symbol, None)
                continue

            state = quote_retry_state.get(symbol, {})
            attempts = int(safe_float_conversion(state.get('attempts')))
            next_retry_ts = safe_float_conversion(state.get('next_retry_ts'))

            if attempts == 0:
                attempts = 1
                next_retry_ts = retry_now_ts + QUOTE_RETRY_COOLDOWN_SECONDS
            elif retry_now_ts >= next_retry_ts:
                attempts += 1
                next_retry_ts = retry_now_ts + QUOTE_RETRY_COOLDOWN_SECONDS

            if attempts > QUOTE_RETRY_MAX_ATTEMPTS:
                quote_retry_state.pop(symbol, None)
                retry_exhausted_symbols.append(symbol)
                continue

            quote_retry_state[symbol] = {
                'attempts': attempts,
                'next_retry_ts': next_retry_ts,
                'last_source': source,
                'updated_ts': retry_now_ts,
            }
            retry_scheduled_symbols.append(symbol)

        if retry_scheduled_symbols:
            logger.info(
                f"Retry queue scheduled for symbols: {sorted(retry_scheduled_symbols)}"
            )
        if retry_exhausted_symbols:
            logger.warning(
                f"Retry queue exhausted for symbols (max {QUOTE_RETRY_MAX_ATTEMPTS}): {sorted(retry_exhausted_symbols)}"
            )

        underlying_df = pd.DataFrame(underlying_data)
        logger.info(f"Created dataframe with {len(underlying_df)} rows")

        logger.info("Calculating metrics...")
        nlv = get_account_value(account_df, 'NetLiquidation', numeric=True, default=0.0)
        gross_pos_val = get_account_value(account_df, 'GrossPositionValue', numeric=True, default=0.0)
        if not is_valid_number(total_npv):
            total_npv = 0.0

        notional_leverage_ratio = total_npv / nlv if nlv > 0 else 0
        standard_leverage_ratio = gross_pos_val / nlv if nlv > 0 else 0

        account_df.loc['NGAV (Notional Gross Asset Value)', 'Value'] = format_currency(total_npv)
        account_df.loc['NLR (Notional Leverage Ratio)', 'Value'] = f"{notional_leverage_ratio:.2f}"
        account_df.loc['Standard Leverage Ratio', 'Value'] = f"{standard_leverage_ratio:.2f}"

        logger.info("Metrics calculated successfully")
        logger.info(f"Portfolio data retrieval complete in {time.time() - started_at:.2f}s")

        health = {
            'as_of': time.time(),
            'connection_ok': bool(ib.isConnected()) and not connection_events,
            'connection_issue_count': len(connection_events),
            'quote_issue_count': len(quote_events),
            'farm_down_count': len(farm_down_names),
            'farm_down_names': farm_down_names,
            'quote_issue_symbols': quote_issue_symbols,
            'fallback_symbols': sorted(fallback_symbols),
        }
        return account_df, underlying_df, positions_by_underlying, health

    except Exception as e:
        logger.error(f"Error in portfolio data retrieval: {str(e)}")
        logger.error(traceback.format_exc())
        return None, None, None, _empty_health(connection_issue_count=1, note=str(e))
