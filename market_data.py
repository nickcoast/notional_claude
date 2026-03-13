import logging

from ib_insync import util

from utils import chunked, is_valid_number, setup_asyncio_event_loop  # noqa: F401 (re-exported)

logger = logging.getLogger(__name__)


def run_async(coro):
    """
    Run async work without creating/closing a brand-new event loop per call.
    Closing loops here caused intermittent timeouts and shutdown errors.
    """
    try:
        return util.run(coro)
    except RuntimeError as runtime_error:
        if "event loop is closed" in str(runtime_error).lower():
            setup_asyncio_event_loop()
            return util.run(coro)
        raise


def pick_price_from_ticker(ticker):
    """Return the best available positive finite price from an IB ticker."""
    if ticker is None:
        return None

    candidates = [ticker.marketPrice(), ticker.last, ticker.close]
    if is_valid_number(ticker.bid) and is_valid_number(ticker.ask):
        candidates.append((float(ticker.bid) + float(ticker.ask)) / 2.0)

    for candidate in candidates:
        if is_valid_number(candidate) and float(candidate) > 0:
            return float(candidate)
    return None


def option_contract_key(contract):
    """Stable key for an option contract across data sources."""
    return (
        contract.symbol,
        contract.lastTradeDateOrContractMonth,
        float(contract.strike),
        contract.right,
        str(contract.multiplier or '100'),
        contract.tradingClass or contract.symbol
    )


def contract_multiplier(contract, default=100):
    """Extract a sane integer multiplier from an IB contract."""
    try:
        if contract.multiplier:
            return int(float(contract.multiplier))
    except (TypeError, ValueError):
        pass
    return default


def option_delta_from_ticker(ticker):
    """Pick the best available delta from IB option greek fields."""
    if ticker is None:
        return None

    for greek_field in ("modelGreeks", "bidGreeks", "askGreeks", "lastGreeks"):
        greeks = getattr(ticker, greek_field, None)
        if not greeks:
            continue
        delta = getattr(greeks, "delta", None)
        if is_valid_number(delta):
            return float(delta)
    return None


def option_underlying_price_from_ticker(ticker):
    """Pick underlying price from option greek fields when available."""
    if ticker is None:
        return None

    for greek_field in ("modelGreeks", "bidGreeks", "askGreeks", "lastGreeks"):
        greeks = getattr(ticker, greek_field, None)
        if not greeks:
            continue
        und_price = getattr(greeks, "undPrice", None)
        if is_valid_number(und_price) and float(und_price) > 0:
            return float(und_price)
    return None


def bounded_req_tickers(ib, contracts, timeout_seconds=2.0, chunk_size=8, label="ticker"):
    """
    Request market data snapshots with bounded timeout/chunking so one slow
    request does not stall the whole fetch cycle.
    """
    if not contracts:
        return []

    tickers = []
    original_timeout = getattr(ib, "RequestTimeout", None)
    try:
        if original_timeout is None or not is_valid_number(original_timeout) or float(original_timeout) > timeout_seconds:
            ib.RequestTimeout = timeout_seconds

        for batch in chunked(contracts, chunk_size):
            try:
                tickers.extend(ib.reqTickers(*batch))
            except Exception as batch_error:
                logger.warning("%s batch request failed for %d contracts: %s", label, len(batch), batch_error)
    finally:
        if original_timeout is not None:
            ib.RequestTimeout = original_timeout

    return tickers


def gather_option_market_data(ib, contracts, wait_seconds=0.6, chunk_size=6):
    """
    Request option market data in short-lived streaming batches.
    This avoids repeated reqTickers timeouts when option snapshots are slow.
    """
    ticker_by_key = {}
    for batch in chunked(contracts, chunk_size):
        active = []
        for contract in batch:
            try:
                ticker = ib.reqMktData(contract, genericTickList='106')
                active.append((contract, ticker))
            except Exception as request_error:
                logger.warning("Option market data request failed for %s: %s", contract.localSymbol, request_error)

        if not active:
            continue

        try:
            ib.sleep(wait_seconds)
        except Exception as sleep_error:
            logger.warning("Option market data wait failed: %s", sleep_error)

        for contract, ticker in active:
            ticker_by_key[option_contract_key(contract)] = ticker

        for contract, _ in active:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass

    return ticker_by_key


def gather_stock_market_data(ib, contracts, wait_seconds=0.5, chunk_size=12):
    """Request stock market data in short-lived streaming batches."""
    ticker_by_symbol = {}
    for batch in chunked(contracts, chunk_size):
        active = []
        for contract in batch:
            try:
                ticker = ib.reqMktData(contract)
                active.append((contract, ticker))
            except Exception as request_error:
                logger.warning("Stock market data request failed for %s: %s", contract.symbol, request_error)

        if not active:
            continue

        try:
            ib.sleep(wait_seconds)
        except Exception as sleep_error:
            logger.warning("Stock market data wait failed: %s", sleep_error)

        for contract, ticker in active:
            ticker_by_symbol[contract.symbol] = ticker

        for contract, _ in active:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass

    return ticker_by_symbol


def gather_stock_snapshot_data(ib, contracts, timeout_seconds=2.0, chunk_size=6):
    """Request stock snapshot data via reqTickers with bounded timeout/chunking."""
    ticker_by_symbol = {}
    tickers = bounded_req_tickers(
        ib,
        contracts,
        timeout_seconds=timeout_seconds,
        chunk_size=chunk_size,
        label="stock snapshot",
    )
    for ticker in tickers:
        contract = getattr(ticker, "contract", None)
        symbol = getattr(contract, "symbol", None)
        if symbol:
            ticker_by_symbol[symbol] = ticker
    return ticker_by_symbol
