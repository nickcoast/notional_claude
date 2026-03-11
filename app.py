import streamlit as st

# MUST be first Streamlit command
st.set_page_config(
    page_title="IB Portfolio Viewer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

import pandas as pd
import numpy as np
import time
import asyncio
from datetime import datetime
import locale
import random

import logging
import traceback
import sys
from functools import wraps
from collections import deque

# Configure logging to write to both console and a file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("ib_app_debug.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger("ib_app")

def time_operation(operation_name):
    """Decorator to time operations and log start/end/error."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            logger.info("Starting %s...", operation_name)
            
            try:
                result = func(*args, **kwargs)
                
                end_time = time.time()
                duration = end_time - start_time
                logger.info("Completed %s in %.2fs", operation_name, duration)
                return result
                
            except Exception as e:
                end_time = time.time()
                duration = end_time - start_time
                logger.error("Error in %s after %.2fs: %s", operation_name, duration, e)
                logger.error(traceback.format_exc())
                raise
        
        return wrapper
    return decorator

# Set the event loop policy first
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

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


def parse_ib_farm_from_error(error_message):
    """Extract farm identifier from IB warning strings like '...:usopt'."""
    if not isinstance(error_message, str):
        return None
    if ":" not in error_message:
        return None
    return error_message.rsplit(":", 1)[-1].strip() or None

def safe_float_conversion(value_str):
    """Safely convert a string to float, handling various formats"""
    if value_str is None:
        return 0.0
    
    # Handle various string formats
    if isinstance(value_str, str):
        # Remove currency symbols and commas
        clean_str = value_str.replace(locale.localeconv()['currency_symbol'], '')
        clean_str = clean_str.replace(',', '')
        try:
            value = float(clean_str)
            return value if np.isfinite(value) else 0.0
        except ValueError:
            logger.warning("Could not convert %r to float", value_str)
            return 0.0
    
    # Already a number
    try:
        value = float(value_str)
        return value if np.isfinite(value) else 0.0
    except (ValueError, TypeError):
        return 0.0


def is_valid_number(value):
    """True when value is a finite numeric scalar."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return np.isfinite(numeric)


def get_account_value(account_df, tag, numeric=False, default=0.0):
    """
    Read account values safely even when duplicate tags exist across accounts.
    numeric=True aggregates duplicate numeric tags by summing them.
    """
    if tag not in account_df.index:
        return default if numeric else None

    value = account_df.loc[tag, 'Value']
    if isinstance(value, pd.Series):
        values = value.tolist()
        if numeric:
            return sum(safe_float_conversion(v) for v in values)
        return values[0] if values else default

    if numeric:
        return safe_float_conversion(value)
    return value


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


def chunked(items, size):
    """Yield fixed-size chunks from a list-like collection."""
    for idx in range(0, len(items), size):
        yield items[idx:idx + size]


def bounded_req_tickers(ib, contracts, timeout_seconds=2.0, chunk_size=8, label="ticker"):
    """
    Request market data snapshots with bounded timeout/chunking so one slow request
    does not stall the whole Streamlit rerun.
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
                logger.warning(f"{label} batch request failed for {len(batch)} contracts: {batch_error}")
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
                logger.warning(f"Option market data request failed for {contract.localSymbol}: {request_error}")

        if not active:
            continue

        try:
            ib.sleep(wait_seconds)
        except Exception as sleep_error:
            logger.warning(f"Option market data wait failed: {sleep_error}")

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
                logger.warning(f"Stock market data request failed for {contract.symbol}: {request_error}")

        if not active:
            continue

        try:
            ib.sleep(wait_seconds)
        except Exception as sleep_error:
            logger.warning(f"Stock market data wait failed: {sleep_error}")

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

# Define the helper function for other threads
def setup_asyncio_event_loop():
    """Ensure there is an event loop available for the current thread"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("Current event loop is closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop

# eventkit (used by ib_insync) expects an event loop during import.
setup_asyncio_event_loop()

# Now import ib_insync after setting up the asyncio environment
from ib_insync import *


def configure_locale():
    """Configure locale with safe fallbacks across environments."""
    for candidate in ("", "en_US.UTF-8", "C.UTF-8", "C"):
        try:
            locale.setlocale(locale.LC_ALL, candidate)
            return locale.setlocale(locale.LC_ALL, None)
        except locale.Error:
            continue
    return None


def format_currency(value):
    """Format currency even when locale-based formatting is unavailable."""
    numeric_value = safe_float_conversion(value)
    try:
        return locale.currency(numeric_value, grouping=True)
    except Exception:
        return f"${numeric_value:,.2f}"


# Set locale for proper currency formatting
active_locale = configure_locale()
if active_locale:
    logger.info(f"Using locale: {active_locale}")
else:
    logger.warning("No supported locale found; currency formatting will use fallback.")

# Global variables - remove threading elements

# Initialize IB connection
@st.cache_resource
def get_ib():
    ib = IB()
    # Avoid indefinite hangs on synchronous request wrappers.
    ib.RequestTimeout = 20
    return ib


@st.cache_resource
def get_runtime_cache():
    """
    Process-lifetime cache for short-lived market data fallbacks.
    Survives Streamlit reruns/browser refreshes, but values are TTL-pruned.
    """
    return {
        'underlying_prices': {},
        'option_deltas': {},
        'quote_retry_state': {},
    }

ib = get_ib()


@st.cache_resource
def get_ib_diagnostics_state():
    """Process-lifetime IB diagnostics captured from API error callbacks."""
    return {
        'events': deque(maxlen=500),
        'farm_status': {},
    }


def register_ib_error_handler():
    """Register a single IB error-event handler for diagnostics."""
    diagnostics_state = get_ib_diagnostics_state()
    if diagnostics_state.get('registered'):
        return

    def handle_ib_error(req_id, error_code, error_message, contract):
        now_ts = time.time()
        symbol = getattr(contract, 'symbol', None) if contract else None
        diagnostics_state['events'].append({
            'ts': now_ts,
            'req_id': req_id,
            'code': int(error_code),
            'message': error_message,
            'symbol': symbol,
        })

        if int(error_code) in IB_FARM_WARNING_CODES:
            farm = parse_ib_farm_from_error(error_message)
            if farm:
                diagnostics_state['farm_status'][farm] = {
                    'code': int(error_code),
                    'message': error_message,
                    'ts': now_ts,
                }

    ib.errorEvent += handle_ib_error
    diagnostics_state['registered'] = True


def get_recent_ib_events(since_ts):
    diagnostics_state = get_ib_diagnostics_state()
    return [event for event in diagnostics_state['events'] if event.get('ts', 0) >= since_ts]


def render_connection_diagnostics():
    """Render sidebar diagnostics that distinguish session vs quote issues."""
    health = st.session_state.get('last_data_health')
    if not health:
        diagnostic_status.info("Diagnostics: waiting for portfolio fetch.")
        diagnostic_detail.empty()
        return

    if not health.get('connection_ok', False):
        note = health.get('note') or "Could not establish/maintain IB API session."
        diagnostic_status.error(f"Diagnostics: {note}")
        diagnostic_detail.empty()
        return

    quote_issue_count = int(health.get('quote_issue_count', 0))
    farm_down_count = int(health.get('farm_down_count', 0))
    if quote_issue_count > 0 or farm_down_count > 0:
        diagnostic_status.warning(
            f"Diagnostics: connected, but quote retrieval degraded (quote issues: {quote_issue_count}, farms down: {farm_down_count})."
        )
        farm_names = health.get('farm_down_names') or []
        if farm_names:
            diagnostic_detail.caption(f"Degraded farms: {', '.join(farm_names[:5])}")
        else:
            diagnostic_detail.empty()
    else:
        diagnostic_status.success("Diagnostics: connected and quote retrieval healthy.")
        diagnostic_detail.empty()

# Connect to IB TWS
@time_operation("IB Connection")
def connect_to_ib():
    """Connect to Interactive Brokers TWS API."""
    logger.info("Starting connection to Interactive Brokers")

    if ib.isConnected():
        logger.info("Already connected to TWS")
        return True

    try:
        client_id = random.randint(1000, 9999)
        logger.info("Attempting connection with client ID: %s", client_id)

        try:
            ib.disconnect()
        except Exception as disconnect_error:
            logger.warning("Disconnect before reconnect failed (non-critical): %s", disconnect_error)

        connect_timeout = 10
        logger.info("Attempting connection to 127.0.0.1:7497 with %ss timeout", connect_timeout)

        connection_start = time.time()
        ib.connect('127.0.0.1', 7497, clientId=client_id, timeout=connect_timeout)
        logger.info("Connection established in %.2fs", time.time() - connection_start)

        try:
            ib.reqMarketDataType(PREFERRED_MARKET_DATA_TYPE)
            logger.info("Market data type set to %s", PREFERRED_MARKET_DATA_TYPE)
        except Exception as market_data_type_error:
            logger.warning("Could not set preferred market data type: %s", market_data_type_error)

        st.success("Connected to Interactive Brokers")

        try:
            account_values = ib.accountSummary()
            if account_values:
                logger.info("Retrieved %s account summary values", len(account_values))
            else:
                logger.warning("Account summary returned empty")
        except Exception as account_error:
            logger.error("Error retrieving account summary: %s", account_error)
            logger.error(traceback.format_exc())

        try:
            positions = ib.positions()
            if positions:
                logger.info("Retrieved %s positions", len(positions))
            else:
                logger.warning("Positions returned empty")
        except Exception as positions_error:
            logger.error("Error retrieving positions: %s", positions_error)
            logger.error(traceback.format_exc())

        logger.info("Connection process completed")
        return True
    except Exception as connect_error:
        logger.error("Unhandled exception during connection: %s", connect_error)
        logger.error(traceback.format_exc())
        return False

# Function to safely run async code
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


async def get_account_summary_compat(ib, timeout=20.0):
    """
    Fetch account summary across ib_insync versions.
    Some versions expose accountSummaryAsync, others only accountSummary.
    """
    if hasattr(ib, "accountSummaryAsync"):
        account_summary_task = asyncio.create_task(ib.accountSummaryAsync())
        return await asyncio.wait_for(account_summary_task, timeout=timeout)
    return ib.accountSummary()


async def get_positions_compat(ib, timeout=20.0):
    """
    Fetch positions across ib_insync versions.
    Some versions expose positionsAsync, others only positions.
    """
    if hasattr(ib, "positionsAsync"):
        positions_task = asyncio.create_task(ib.positionsAsync())
        return await asyncio.wait_for(positions_task, timeout=timeout)
    return ib.positions()

# Async wrapper for portfolio data with improved debugging and timeout handling
@time_operation("Portfolio Data Retrieval")
async def async_get_portfolio_data(ib):
    try:
        # Debug info
        logger.info("Starting portfolio data retrieval")
        try:
            ib.reqMarketDataType(PREFERRED_MARKET_DATA_TYPE)
        except Exception as market_data_type_error:
            logger.warning(f"Unable to set market data type {PREFERRED_MARKET_DATA_TYPE}: {market_data_type_error}")
        
        # Get account summary with timeout
        logger.info("Fetching account data...")
        
        try:
            # Fetch account summary with a timeout
            account_summary = await get_account_summary_compat(ib, timeout=20.0)
            
            if not account_summary:
                logger.warning("Account summary is empty")
                return None, None, None
                
            logger.info(f"Got {len(account_summary)} account values")
            
            account_df = pd.DataFrame([(row.tag, row.value) for row in account_summary], 
                                columns=['Tag', 'Value'])
            account_df = account_df.set_index('Tag')
            
            # Update debug state
            
        except asyncio.TimeoutError:
            logger.error("Timeout occurred while waiting for account data (20s)")
            return None, None, None
        except Exception as account_error:
            logger.error(f"Error getting account data: {account_error}")
            logger.error(traceback.format_exc())
            return None, None, None
        
        # Get positions with timeout
        logger.info("Fetching positions...")
        
        try:
            # Fetch positions with a timeout
            positions = await get_positions_compat(ib, timeout=20.0)
            positions = positions or []
            if not positions:
                logger.warning("No positions found")
            else:
                logger.info(f"Got {len(positions)} positions")
            
        except asyncio.TimeoutError:
            logger.error("Timeout occurred while waiting for position data (20s)")
            positions = []
        except Exception as positions_error:
            logger.error(f"Error getting positions: {positions_error}")
            logger.error(traceback.format_exc())
            positions = []
        
        # Create a dictionary to store positions by underlying
        positions_by_underlying = {}
        underlying_price_cache = {}
        
        # Process positions
        logger.info("Processing positions...")
        position_count = 0
        position_errors = 0
        
        # Process each position with individual error handling
        for pos in positions:
            try:
                position_count += 1
                contract = pos.contract
                underlying_symbol = contract.symbol
                
                logger.debug(f"Processing position {position_count}/{len(positions)}: {underlying_symbol}")
                
                if underlying_symbol in underlying_price_cache:
                    underlying_price = underlying_price_cache[underlying_symbol]
                else:
                    # Get market price once per underlying symbol.
                    if contract.secType == 'STK':
                        underlying_contract = contract
                    else:
                        underlying_contract = Stock(underlying_symbol, 'SMART', 'USD')

                    try:
                        ticker = ib.reqMktData(underlying_contract)
                        underlying_price = None
                        for _ in range(8):
                            underlying_price = pick_price_from_ticker(ticker)
                            if underlying_price is not None:
                                break
                            await asyncio.sleep(0.15)

                        # Fallback to snapshot-style request if streaming fields are empty.
                        if underlying_price is None:
                            tickers = ib.reqTickers(underlying_contract)
                            if tickers:
                                underlying_price = pick_price_from_ticker(tickers[0])
                    except Exception as ticker_error:
                        logger.warning(f"Error requesting market data for {underlying_symbol}: {ticker_error}")
                        position_errors += 1
                        continue
                    finally:
                        try:
                            ib.cancelMktData(underlying_contract)
                        except Exception:
                            pass

                    if underlying_price is None:
                        # Use average cost as a last resort instead of NaN/None.
                        fallback_price = safe_float_conversion(pos.avgCost)
                        if fallback_price > 0:
                            underlying_price = fallback_price
                            logger.warning(f"No market price for {underlying_symbol}, using avg cost: {underlying_price}")
                        else:
                            underlying_price = 100.0
                            logger.warning(f"No price data for {underlying_symbol}, using 100 placeholder")

                    underlying_price_cache[underlying_symbol] = underlying_price
                
                if position_count <= 5:  # Show debug for first few positions
                    logger.debug(f"Position {position_count}: {underlying_symbol} @ {underlying_price}")
                
                if underlying_symbol not in positions_by_underlying:
                    positions_by_underlying[underlying_symbol] = {
                        'stock_count': 0,
                        'stock_value': 0,
                        'option_notional': 0,
                        'option_actual_value': 0,
                        'underlying_price': underlying_price
                    }
                
                # Calculate position values with detailed logging
                if contract.secType == 'STK':
                    positions_by_underlying[underlying_symbol]['stock_count'] += pos.position
                    positions_by_underlying[underlying_symbol]['stock_value'] += pos.position * underlying_price
                elif contract.secType == 'OPT':
                    # Process option with timeout and error handling
                    try:
                        await process_option_position(ib, contract, pos, underlying_symbol, underlying_price, positions_by_underlying)
                    except Exception as option_error:
                        logger.warning(f"Error processing option {contract.symbol}: {option_error}")
                        position_errors += 1
                        continue
            
            except Exception as position_error:
                logger.warning(f"Error processing position {position_count}: {position_error}")
                position_errors += 1
                continue
        
        if position_errors > 0:
            logger.warning(f"Encountered errors in {position_errors}/{position_count} positions")
        
        logger.info("Creating dataframe...")
        
        # Create DataFrame for display
        underlying_data = []
        total_npv = 0
        
        for symbol, data in positions_by_underlying.items():
            try:
                stock_notional = data['stock_count'] * data['underlying_price']
                option_notional = data['option_notional'] * data['underlying_price']
                total_notional = stock_notional + option_notional
                
                underlying_data.append({
                    'Symbol': symbol,
                    'Stock Count': data['stock_count'],
                    'Stock Value': data['stock_value'],
                    'Option Notional (Shares)': data['option_notional'],
                    'Option Notional Value': option_notional,
                    'Option Actual Value': data['option_actual_value'],
                    'Underlying Price': data['underlying_price'],
                    'Notional Position Value (NPV)': total_notional
                })
                
                total_npv += total_notional
            except Exception as calc_error:
                logger.warning(f"Error calculating values for {symbol}: {calc_error}")
                continue
        
        underlying_df = pd.DataFrame(underlying_data)
        logger.info(f"Created dataframe with {len(underlying_df)} rows")
        
        # Calculate portfolio metrics
        logger.info("Calculating metrics...")
        try:
            nlv = get_account_value(account_df, 'NetLiquidation', numeric=True, default=0.0)
            gross_pos_val = get_account_value(account_df, 'GrossPositionValue', numeric=True, default=0.0)

            if not is_valid_number(total_npv):
                total_npv = 0.0
            
            # Calculate notional leverage ratio
            notional_leverage_ratio = total_npv / nlv if nlv > 0 else 0
            standard_leverage_ratio = gross_pos_val / nlv if nlv > 0 else 0
            
            # Add NGAV and NLR to account summary
            account_df.loc['NGAV (Notional Gross Asset Value)', 'Value'] = format_currency(total_npv)
            account_df.loc['NLR (Notional Leverage Ratio)', 'Value'] = f"{notional_leverage_ratio:.2f}"
            account_df.loc['Standard Leverage Ratio', 'Value'] = f"{standard_leverage_ratio:.2f}"
            
            logger.info("Metrics calculated successfully")
        except Exception as metrics_error:
            logger.error(f"Error calculating metrics: {metrics_error}")
            logger.error(traceback.format_exc())
            # Handle case where account data doesn't have the expected fields
            pass
        
        logger.info("Portfolio data retrieval complete")
        return account_df, underlying_df, positions_by_underlying
        
    except Exception as e:
        logger.error(f"Error in portfolio data retrieval: {str(e)}")
        logger.error(traceback.format_exc())
        return None, None, None

# Helper function to process option positions
async def process_option_position(ib, contract, pos, underlying_symbol, underlying_price, positions_by_underlying):
    logger.debug(f"Processing option: {contract.symbol} {contract.right} {contract.strike}")
    
    # Get option data with timeout
    try:
        option_ticker_task = ib.reqMktData(contract)
        await asyncio.sleep(0.2)  # Small delay to respect rate limits
    except Exception as ticker_error:
        logger.warning(f"Error requesting option market data: {ticker_error}")
        raise
    
    # Calculate option delta (if available, otherwise use approximation)
    delta = None
    option_price = pick_price_from_ticker(option_ticker_task) or 0.0

    # Allow a short window for greeks to populate before falling back.
    for _ in range(6):
        if hasattr(option_ticker_task, 'modelGreeks') and option_ticker_task.modelGreeks:
            model_delta = option_ticker_task.modelGreeks.delta
            if is_valid_number(model_delta):
                delta = float(model_delta)
                break
        await asyncio.sleep(0.1)

    logger.debug(f"Got delta from model Greeks: {delta}")

    # Fallback delta calculation when model greeks are unavailable or invalid.
    if delta is None:
        if contract.right == 'C':  # Call option
            delta = 0.7 if underlying_price > contract.strike else 0.3
        else:  # Put option
            delta = -0.7 if underlying_price < contract.strike else -0.3
        logger.debug(f"Using fallback delta: {delta}")
    
    # Signed share-equivalent notional (puts are negative delta).
    option_multiplier = 100
    option_notional = delta * option_multiplier * pos.position
    positions_by_underlying[underlying_symbol]['option_notional'] += option_notional
    
    # Signed actual option value (short option value is negative).
    option_value = option_price * option_multiplier * pos.position
    positions_by_underlying[underlying_symbol]['option_actual_value'] += option_value
    
    try:
        ib.cancelMktData(contract)
    except Exception:
        pass

    logger.debug(f"Option processed: notional={option_notional}, value={option_value}")

@time_operation("Portfolio Data Retrieval")
def get_portfolio_data_sync(ib):
    """
    Main synchronous portfolio data path.
    Keeps IB requests on one thread/loop context to avoid nested event-loop errors.
    """
    logger.info("Starting portfolio data retrieval")
    try:
        started_at = time.time()
        st.session_state['last_data_health'] = {
            'as_of': started_at,
            'connection_ok': bool(ib.isConnected()),
            'connection_issue_count': 0,
            'quote_issue_count': 0,
            'farm_down_count': 0,
            'farm_down_names': [],
            'quote_issue_symbols': [],
            'fallback_symbols': [],
        }
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
            st.session_state['last_data_health'] = {
                'as_of': time.time(),
                'connection_ok': False,
                'connection_issue_count': 1,
                'quote_issue_count': 0,
                'farm_down_count': 0,
                'farm_down_names': [],
                'quote_issue_symbols': [],
                'fallback_symbols': [],
                'note': 'Empty account summary from IB API',
            }
            return None, None, None

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
        runtime_cache = get_runtime_cache()
        persistent_underlying_price_cache = runtime_cache['underlying_prices']
        persistent_option_delta_cache = runtime_cache['option_deltas']
        quote_retry_state = runtime_cache['quote_retry_state']
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
        session_delta_cache = st.session_state.setdefault('option_delta_cache', {})
        session_underlying_price_cache = st.session_state.setdefault('underlying_price_cache', {})
        delta_cache = unpack_numeric_cache(persistent_option_delta_cache)
        delta_cache.update(session_delta_cache)
        underlying_price_cache = unpack_numeric_cache(persistent_underlying_price_cache)
        underlying_price_cache.update(session_underlying_price_cache)

        def cache_option_delta(key, delta_value):
            if not is_valid_number(delta_value):
                return
            numeric_delta = float(delta_value)
            delta_cache[key] = numeric_delta
            session_delta_cache[key] = numeric_delta
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
            underlying_price_cache[symbol] = numeric_price
            session_underlying_price_cache[symbol] = numeric_price
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
        force_retry_now = bool(st.session_state.pop('force_retry_quotes_now', False))
        auto_retry_enabled = bool(st.session_state.get('auto_retry_quotes_enabled', False))
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
                            known_market_price = underlying_price_cache.get(underlying_symbol)
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
                    cached_price = underlying_price_cache.get(symbol)
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

        st.session_state['last_data_health'] = {
            'as_of': time.time(),
            'connection_ok': bool(ib.isConnected()) and not connection_events,
            'connection_issue_count': len(connection_events),
            'quote_issue_count': len(quote_events),
            'farm_down_count': len(farm_down_names),
            'farm_down_names': farm_down_names,
            'quote_issue_symbols': quote_issue_symbols,
            'fallback_symbols': sorted(fallback_symbols),
        }

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
        return account_df, underlying_df, positions_by_underlying

    except Exception as e:
        logger.error(f"Error in portfolio data retrieval: {str(e)}")
        logger.error(traceback.format_exc())
        return None, None, None

# Async wrapper for option chain data
async def async_get_option_chain(ib, ticker):
    # Get the stock contract
    stock = Stock(ticker, 'SMART', 'USD')
    await ib.qualifyContractsAsync(stock)
    
    # Get current stock price
    ticker = ib.reqMktData(stock)
    await asyncio.sleep(0.2)
    stock_price = ticker.marketPrice()
    
    # Get the option chains
    chains = await ib.reqSecDefOptParamsAsync(stock.symbol, '', stock.secType, stock.conId)
    
    # Get all expiration dates
    expirations = []
    for chain in chains:
        if chain.exchange == 'SMART':
            expirations = sorted(chain.expirations)
            break
    
    # Return all data needed
    return stock_price, expirations

# Async wrapper for options data
async def async_get_options_for_expiration(ib, ticker, expiration):
    # Get the stock contract
    stock = Stock(ticker, 'SMART', 'USD')
    await ib.qualifyContractsAsync(stock)
    
    # Get current stock price
    ticker_data = ib.reqMktData(stock)
    await asyncio.sleep(0.2)
    stock_price = ticker_data.marketPrice()
    
    # Get option chain for selected expiration
    chains = await ib.reqSecDefOptParamsAsync(stock.symbol, '', stock.secType, stock.conId)
    
    # Find the SMART exchange chain
    chain = next((c for c in chains if c.exchange == 'SMART'), None)
    if not chain:
        return None, None, None
    
    # Get all strike prices
    strikes = sorted(chain.strikes)
    
    # Create call and put options
    calls = []
    puts = []
    
    # Request data for each strike
    for strike in strikes:
        call_contract = Option(ticker, expiration, strike, 'C', 'SMART')
        put_contract = Option(ticker, expiration, strike, 'P', 'SMART')
        
        await ib.qualifyContractsAsync(call_contract, put_contract)
        
        # Request market data for call
        call_ticker = ib.reqMktData(call_contract)
        await asyncio.sleep(0.1)  # Small delay to respect rate limits
        
        # Request market data for put
        put_ticker = ib.reqMktData(put_contract)
        await asyncio.sleep(0.1)  # Small delay
        
        # Get data for call
        call_price = call_ticker.marketPrice()
        call_bid = call_ticker.bid
        call_ask = call_ticker.ask
        call_last = call_ticker.last
        
        # Try to get delta and gamma for call
        call_delta = None
        call_gamma = None
        
        if hasattr(call_ticker, 'modelGreeks') and call_ticker.modelGreeks:
            call_delta = call_ticker.modelGreeks.delta
            call_gamma = call_ticker.modelGreeks.gamma
        else:
            # Use approximation
            call_delta = 0.7 if stock_price > strike else 0.3
            call_gamma = 0.01  # Default gamma
        
        # Similarly for put
        put_price = put_ticker.marketPrice()
        put_bid = put_ticker.bid
        put_ask = put_ticker.ask
        put_last = put_ticker.last
        
        put_delta = None
        put_gamma = None
        
        if hasattr(put_ticker, 'modelGreeks') and put_ticker.modelGreeks:
            put_delta = put_ticker.modelGreeks.delta
            put_gamma = put_ticker.modelGreeks.gamma
        else:
            # Use approximation
            put_delta = -0.7 if stock_price < strike else -0.3
            put_gamma = 0.01  # Default gamma
        
        # Calculate percentage of stock price
        call_pct = (call_price / stock_price) * 100 if stock_price > 0 else 0
        put_pct = (put_price / stock_price) * 100 if stock_price > 0 else 0
        
        # Calculate difference from stock price
        call_diff = call_price - (stock_price - strike) if stock_price > strike else call_price
        put_diff = put_price - (strike - stock_price) if stock_price < strike else put_price
        
        calls.append({
            'Strike': strike,
            'Bid': call_bid,
            'Ask': call_ask,
            'Last': call_last,
            'Price': call_price,
            'Delta': call_delta,
            'Gamma': call_gamma,
            'Pct of Stock': f"{call_pct:.2f}%",
            'Diff from Stock': call_diff
        })
        
        puts.append({
            'Strike': strike,
            'Bid': put_bid,
            'Ask': put_ask,
            'Last': put_last,
            'Price': put_price,
            'Delta': put_delta,
            'Gamma': put_gamma,
            'Pct of Stock': f"{put_pct:.2f}%",
            'Diff from Stock': put_diff
        })
    
    return stock_price, calls, puts

# Non-async wrapper functions for threading
def get_portfolio_data():
    if not ib.isConnected():
        return None, None, None
    try:
        return get_portfolio_data_sync(ib)
    except Exception as e:
        st.error(f"Error getting portfolio data: {e}")
        return None, None, None

def get_option_chain(ticker):
    if not ib.isConnected():
        return None, None
    try:
        return run_async(async_get_option_chain(ib, ticker))
    except Exception as e:
        st.error(f"Error getting option chain: {e}")
        return None, None

def get_options_for_expiration(ticker, expiration):
    if not ib.isConnected():
        return None, None, None
    try:
        return run_async(async_get_options_for_expiration(ib, ticker, expiration))
    except Exception as e:
        st.error(f"Error getting options data: {e}")
        return None, None, None

# UI Layout - Main App
st.title("Interactive Brokers Portfolio Viewer")

# Status indicator in sidebar
st.sidebar.title("IB Connection Status")
connection_status = st.sidebar.empty()
diagnostic_status = st.sidebar.empty()
diagnostic_detail = st.sidebar.empty()

if not ib.isConnected():
    if st.sidebar.button("Connect to TWS"):
        connect_to_ib()

# Update connection status
if ib.isConnected():
    connection_status.success("Connected to TWS")
else:
    connection_status.error("Not connected to TWS")

# Portfolio-wide metrics section (always visible)
st.header("Portfolio Metrics")
portfolio_metrics = st.empty()

# Main content area
main_content = st.container()

# Options Browser
st.header("Options Browser")
search_col, expiry_col = st.columns([1, 3])
with search_col:
    ticker_input = st.text_input("Enter Ticker Symbol", "")
    search_button = st.button("Search Options")

expiration_container = expiry_col.empty()
options_display = st.empty()

# Auto-refresh control (using st.rerun)
auto_refresh = st.sidebar.checkbox("Auto Refresh", value=False)
refresh_interval = None
if auto_refresh:
    refresh_interval = st.sidebar.slider("Refresh Interval (seconds)", 5, 60, 10)

auto_retry_quotes = st.sidebar.checkbox("Auto Retry Weak Quotes", value=False)
st.session_state['auto_retry_quotes_enabled'] = auto_retry_quotes
if st.sidebar.button("Retry Weak Quotes Now"):
    st.session_state['force_retry_quotes_now'] = True
    st.rerun()
st.sidebar.caption(f"Market data type: {PREFERRED_MARKET_DATA_TYPE}")

if st.sidebar.button("Clear Quote Cache"):
    runtime_cache = get_runtime_cache()
    runtime_cache['underlying_prices'].clear()
    runtime_cache['option_deltas'].clear()
    runtime_cache['quote_retry_state'].clear()
    st.session_state.pop('underlying_price_cache', None)
    st.session_state.pop('option_delta_cache', None)
    st.sidebar.success("Cleared quote cache.")

retry_state_preview = get_runtime_cache().get('quote_retry_state', {})
if retry_state_preview:
    retry_symbols = sorted(retry_state_preview.keys())
    preview = ", ".join(retry_symbols[:6])
    if len(retry_symbols) > 6:
        preview = f"{preview}, ..."
    st.sidebar.caption(f"Pending quote retries ({len(retry_symbols)}): {preview}")
    if auto_retry_quotes and not auto_refresh:
        st.sidebar.caption("Retries run on each manual refresh/click. Enable Auto Refresh for continuous retry cycles.")

# Streamlit-compatible main execution
def main():
    """Main function that runs within Streamlit's execution model"""
    setup_asyncio_event_loop()
    register_ib_error_handler()
    
    logger.info("Starting application")
    
    # Try to connect if not already connected
    if not ib.isConnected():
        logger.info("Attempting connection to TWS")
        try:
            connection_success = connect_to_ib()
        except Exception as conn_error:
            logger.error(f"Unhandled exception in connect_to_ib: {conn_error}")
            logger.error(traceback.format_exc())
            connection_success = False
    else:
        connection_success = True

    # Refresh sidebar indicator after connection logic runs in this same rerun.
    if ib.isConnected():
        connection_status.success("Connected to TWS")
    else:
        connection_status.error("Not connected to TWS")
        st.session_state['last_data_health'] = {
            'as_of': time.time(),
            'connection_ok': False,
            'connection_issue_count': 1,
            'quote_issue_count': 0,
            'farm_down_count': 0,
            'farm_down_names': [],
            'quote_issue_symbols': [],
            'fallback_symbols': [],
            'note': 'IB API session not connected',
        }
    render_connection_diagnostics()
    
    if connection_success:
        # Update portfolio data directly (no background threads)
        try:
            account_df, underlying_df, _ = get_portfolio_data()
            
            if account_df is not None and underlying_df is not None:
                # Update portfolio metrics display
                with portfolio_metrics.container():
                    try:
                        # Create a nice grid layout for the metrics
                        metrics_cols = st.columns(6)
                        
                        # Extract key metrics
                        try:
                            nlv = get_account_value(account_df, 'NetLiquidation', numeric=True, default=0.0)
                            gross_pos_val = get_account_value(account_df, 'GrossPositionValue', numeric=True, default=0.0)
                            ngav = get_account_value(account_df, 'NGAV (Notional Gross Asset Value)', numeric=True, default=0.0)
                            nlr = get_account_value(account_df, 'NLR (Notional Leverage Ratio)', numeric=True, default=0.0)
                            std_leverage = get_account_value(account_df, 'Standard Leverage Ratio', numeric=True, default=0.0)
                            buying_power = get_account_value(account_df, 'BuyingPower', numeric=True, default=0.0)
                            
                            metrics_cols[0].metric("Net Liquidation Value", 
                                                 format_currency(nlv))
                            metrics_cols[1].metric("Gross Position Value", 
                                                 format_currency(gross_pos_val))
                            metrics_cols[2].metric("NGAV", 
                                                 format_currency(ngav))
                            metrics_cols[3].metric("Standard Leverage", 
                                                 f"{std_leverage:.2f}x")
                            metrics_cols[4].metric("Notional Leverage Ratio", 
                                                 f"{nlr:.2f}x")
                            metrics_cols[5].metric("Buying Power", 
                                                 format_currency(buying_power))
                        except Exception as e:
                            logger.error(f"Error updating metrics: {e}")
                    except Exception as container_error:
                        logger.error(f"Error with metrics container: {container_error}")
                
                # Update underlying positions table
                with main_content:
                    try:
                        st.subheader("Positions by Underlying")
                        # Keep numeric dtypes for correct sorting; format via column_config.
                        display_df = underlying_df.copy()
                        numeric_cols = [
                            'Stock Count',
                            'Stock Value',
                            'Option Notional (Shares)',
                            'Option Notional Value',
                            'Option Actual Value',
                            'Underlying Market Price',
                            'Underlying Cost Basis',
                            'Underlying Price',
                            'Notional Position Value (NPV)',
                        ]
                        for col in numeric_cols:
                            if col in display_df.columns:
                                display_df[col] = pd.to_numeric(display_df[col], errors='coerce')

                        column_config = {}
                        currency_cols = [
                            'Stock Value',
                            'Option Notional Value',
                            'Option Actual Value',
                            'Underlying Market Price',
                            'Underlying Cost Basis',
                            'Underlying Price',
                            'Notional Position Value (NPV)',
                        ]
                        for col in currency_cols:
                            if col in display_df.columns:
                                column_config[col] = st.column_config.NumberColumn(
                                    col,
                                    format="$%.2f",
                                )

                        if 'Option Notional (Shares)' in display_df.columns:
                            column_config['Option Notional (Shares)'] = st.column_config.NumberColumn(
                                'Option Notional (Shares)',
                                format="%.2f",
                            )
                        if 'Stock Count' in display_df.columns:
                            column_config['Stock Count'] = st.column_config.NumberColumn(
                                'Stock Count',
                                format="%.4f",
                            )

                        st.dataframe(display_df, use_container_width=True, column_config=column_config)
                        
                        # Show last update time
                        st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    except Exception as table_error:
                        logger.error(f"Error updating positions table: {table_error}")
        except Exception as update_error:
            logger.error(f"Error updating portfolio data: {update_error}")
            
        # Handle options data
        if ticker_input and search_button:
            try:
                stock_price, expirations = get_option_chain(ticker_input)
                
                if stock_price is not None and expirations:
                    # Display expiration selection
                    with expiration_container.container():
                        # Format expirations for display
                        exp_dates = [datetime.strptime(exp, '%Y%m%d').strftime('%Y-%m-%d') for exp in expirations]
                        selected_exp_index = st.select_slider(
                            "Select Expiration Date",
                            options=range(len(exp_dates)),
                            format_func=lambda i: exp_dates[i]
                        )
                        selected_exp = expirations[selected_exp_index]
                    
                    # Get options data for selected expiration
                    stock_price, calls, puts = get_options_for_expiration(ticker_input, selected_exp)
                    
                    if stock_price is not None and calls and puts:
                        # Display options data
                        with options_display.container():
                            st.subheader(f"{ticker_input} Options - Stock Price: ${stock_price:.2f}")
                            
                            # Create DataFrame for calls and puts
                            calls_df = pd.DataFrame(calls)
                            puts_df = pd.DataFrame(puts)
                            
                            # Display tables side by side with strike in the middle
                            cols = st.columns([4, 2, 4])
                            
                            # Display calls on the left
                            with cols[0]:
                                st.subheader("CALLS")
                                call_display_cols = ['Bid', 'Ask', 'Last', 'Price', 'Delta', 'Gamma', 'Pct of Stock', 'Diff from Stock']
                                st.dataframe(calls_df[call_display_cols], use_container_width=True)
                            
                            # Display strikes in the middle
                            with cols[1]:
                                st.subheader("Strike")
                                st.dataframe(calls_df[['Strike']], use_container_width=True)
                            
                            # Display puts on the right
                            with cols[2]:
                                st.subheader("PUTS")
                                put_display_cols = ['Bid', 'Ask', 'Last', 'Price', 'Delta', 'Gamma', 'Pct of Stock', 'Diff from Stock']
                                st.dataframe(puts_df[put_display_cols], use_container_width=True)
                            
                            # Show last update time
                            st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception as options_error:
                logger.error(f"Error updating options data: {options_error}")

        # Refresh diagnostics after data fetch updates health state.
        render_connection_diagnostics()

        if auto_refresh and refresh_interval:
            logger.debug("Auto refresh rerun in %ss", refresh_interval)
            time.sleep(refresh_interval)
            st.rerun()
    else:
        st.error("Failed to connect to Interactive Brokers. Please make sure TWS or IB Gateway is running.")

# Main execution - simplified for Streamlit
if __name__ == "__main__":
    # Ensure event loop is set up
    setup_asyncio_event_loop()
    
    # Display info
    st.sidebar.info("""
    This app connects to Interactive Brokers TWS API to display portfolio data.
    Make sure TWS or IB Gateway is running before connecting.
    """)
    
    # Manual refresh button
    if st.sidebar.button("Refresh Data"):
        st.rerun()
    
    # Run the main function
    main()
