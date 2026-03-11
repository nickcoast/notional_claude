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
import threading
import asyncio
from datetime import datetime
import locale
import random

"""
Debug helper functions for Interactive Brokers API integration
"""
import logging
import traceback
import sys
from functools import wraps

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

# Dictionary to store debug state
debug_state = {
    'last_operation': None,
    'last_error': None,
    'connection_attempts': 0,
    'api_timings': {},
    'account_data_received': False,
    'positions_received': False,
    'last_update_time': None
}

def log_debug(message, level="info", display_ui=True, ui_container=None):
    """
    Log a debug message to both the log file and optionally the UI
    
    Args:
        message: The message to log
        level: Log level (debug, info, warning, error, critical)
        display_ui: Whether to display in the UI
        ui_container: Streamlit container to write to (if None, uses st.sidebar)
    """
    # Log to file/console
    log_func = getattr(logger, level.lower())
    log_func(message)
    
    # Update debug state
    debug_state['last_operation'] = message
    debug_state['last_update_time'] = datetime.now()
    
    # Display in UI if requested
    if display_ui and 'st' in globals():
        container = ui_container if ui_container else st.sidebar
        
        if level.lower() == "error":
            container.error(message)
            debug_state['last_error'] = message
        elif level.lower() == "warning":
            container.warning(message)
        else:
            container.info(message)

def time_operation(operation_name):
    """
    Decorator to time API operations and log the results
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            log_debug(f"Starting {operation_name}...", "info")
            
            try:
                result = func(*args, **kwargs)
                
                end_time = time.time()
                duration = end_time - start_time
                debug_state['api_timings'][operation_name] = {
                    'last_duration': duration,
                    'last_success': True,
                    'timestamp': datetime.now()
                }
                
                log_debug(f"Completed {operation_name} in {duration:.2f}s", "info")
                return result
                
            except Exception as e:
                end_time = time.time()
                duration = end_time - start_time
                debug_state['api_timings'][operation_name] = {
                    'last_duration': duration,
                    'last_success': False,
                    'timestamp': datetime.now(),
                    'error': str(e)
                }
                
                log_debug(f"Error in {operation_name} after {duration:.2f}s: {str(e)}", "error")
                log_debug(traceback.format_exc(), "error", display_ui=False)
                raise
        
        return wrapper
    return decorator

async def timeout_async(coro, timeout=10.0, operation_name="async operation"):
    """
    Run an async coroutine with a timeout
    """
    try:
        log_debug(f"Starting async {operation_name} with {timeout}s timeout", "info")
        # Create a task for the coroutine
        task = asyncio.create_task(coro)
        
        # Wait for the task to complete with a timeout
        result = await asyncio.wait_for(task, timeout=timeout)
        log_debug(f"Async {operation_name} completed successfully", "info")
        return result
    except asyncio.TimeoutError:
        log_debug(f"Timeout occurred in {operation_name} after {timeout}s", "error")
        raise
    except Exception as e:
        log_debug(f"Exception in {operation_name}: {str(e)}", "error")
        log_debug(traceback.format_exc(), "error", display_ui=False)
        raise

def display_debug_panel():
    """
    Display a debug panel in the Streamlit sidebar
    """
    with st.sidebar.expander("Debug Information", expanded=False):
        st.write("### Connection Status")
        if ib.isConnected():
            st.success(f"Connected to TWS (Client ID: {ib.client.clientId})")
        else:
            st.error("Not connected to TWS")
            
        st.write("### Last Operations")
        st.text(f"Last operation: {debug_state['last_operation']}")
        if debug_state['last_error']:
            st.error(f"Last error: {debug_state['last_error']}")
            
        st.write("### Account Data Status")
        st.text(f"Account data received: {debug_state['account_data_received']}")
        st.text(f"Positions received: {debug_state['positions_received']}")
        
        st.write("### API Timing Information")
        for op_name, timing in debug_state['api_timings'].items():
            status = "✅" if timing['last_success'] else "❌"
            st.text(f"{op_name}: {status} {timing['last_duration']:.2f}s")
            st.text(f"  Time: {timing['timestamp'].strftime('%H:%M:%S')}")
            if not timing['last_success'] and 'error' in timing:
                st.text(f"  Error: {timing['error']}")
                
        st.write("### Debug Controls")
        if st.button("Force Reconnect"):
            # Use thread-safe way to request reconnection
            st.session_state.force_reconnect = True
"""
END: Debug helper functions for Interactive Brokers API integration
"""

# Set the event loop policy first
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

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
            st.sidebar.warning(f"Could not convert '{value_str}' to float")
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
                log_debug(f"{label} batch request failed for {len(batch)} contracts: {batch_error}", "warning")
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
                log_debug(f"Option market data request failed for {contract.localSymbol}: {request_error}", "warning")

        if not active:
            continue

        try:
            ib.sleep(wait_seconds)
        except Exception as sleep_error:
            log_debug(f"Option market data wait failed: {sleep_error}", "warning")

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
                log_debug(f"Stock market data request failed for {contract.symbol}: {request_error}", "warning")

        if not active:
            continue

        try:
            ib.sleep(wait_seconds)
        except Exception as sleep_error:
            log_debug(f"Stock market data wait failed: {sleep_error}", "warning")

        for contract, ticker in active:
            ticker_by_symbol[contract.symbol] = ticker

        for contract, _ in active:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass

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
    }

ib = get_ib()

# Connect to IB TWS
@time_operation("IB Connection")
def connect_to_ib():
    """Connect to Interactive Brokers TWS API with extensive debugging"""
    debug_container = st.sidebar.empty()
    log_debug("Starting connection to Interactive Brokers", ui_container=debug_container)
    
    debug_state['connection_attempts'] += 1
    
    if not ib.isConnected():
        try:
            # Use a random client ID to avoid conflicts
            client_id = random.randint(1000, 9999)
            log_debug(f"Attempting connection with client ID: {client_id}", ui_container=debug_container)
            
            # Try to disconnect first in case of lingering connections
            try:
                log_debug("Disconnecting any existing connections", ui_container=debug_container)
                ib.disconnect()
                log_debug("Disconnect successful", ui_container=debug_container)
            except Exception as disconnect_error:
                log_debug(f"Disconnect error (non-critical): {disconnect_error}", "warning", ui_container=debug_container)
            
            # Clear cached data
            log_debug("Clearing any cached data", ui_container=debug_container)
            debug_state['account_data_received'] = False
            debug_state['positions_received'] = False
            
            # Connect with timeout handling
            connect_timeout = 10  # seconds
            log_debug(f"Attempting connection to 127.0.0.1:7497 with {connect_timeout}s timeout", ui_container=debug_container)
            
            # The connection itself (non-async)
            connection_start = time.time()
            try:
                ib.connect('127.0.0.1', 7497, clientId=client_id, timeout=connect_timeout)
                connection_duration = time.time() - connection_start
                log_debug(f"Connection established in {connection_duration:.2f}s", ui_container=debug_container)
                try:
                    # Use delayed-frozen market data when live subscriptions are unavailable.
                    ib.reqMarketDataType(4)
                    log_debug("Market data type set to delayed-frozen (4)", ui_container=debug_container)
                except Exception as market_data_type_error:
                    log_debug(f"Could not set delayed market data type: {market_data_type_error}", "warning", ui_container=debug_container)
            except Exception as connect_error:
                log_debug(f"Connection failed: {connect_error}", "error", ui_container=debug_container)
                log_debug(traceback.format_exc(), "error", display_ui=False)
                return False
            
            # Connection succeeded, now check account data
            st.success("Connected to Interactive Brokers")
            
            # Add diagnostic information
            log_debug("Checking account data availability...", ui_container=debug_container)
            
            # Test if we can get account info
            try:
                log_debug("Requesting account summary data...", ui_container=debug_container)
                account_values = ib.accountSummary()

                if account_values:
                    debug_state['account_data_received'] = True
                    log_debug(f"Successfully retrieved {len(account_values)} account values", "info", ui_container=debug_container)

                    # Display a sample of key values for diagnostics
                    account_sample = [val for val in account_values if val.tag in ['NetLiquidation', 'GrossPositionValue', 'TotalCashValue']]
                    if account_sample:
                        sample_text = ""
                        for val in account_sample:
                            sample_text += f"{val.tag}: {val.value}\n"
                        log_debug(f"Account sample:\n{sample_text}", ui_container=debug_container)
                    else:
                        log_debug("No sample account values found in expected categories", "warning", ui_container=debug_container)
                else:
                    log_debug("Account data returned empty. Check permissions in IB Gateway.", "warning", ui_container=debug_container)
            except Exception as e:
                log_debug(f"Error retrieving account data: {e}", "error", ui_container=debug_container)
                log_debug(traceback.format_exc(), "error", display_ui=False)
                # Continue anyway - we might still be able to get positions

            # Test if we can get positions
            try:
                log_debug("Requesting position data...", ui_container=debug_container)
                positions = ib.positions()

                if positions:
                    debug_state['positions_received'] = True
                    log_debug(f"Successfully retrieved {len(positions)} positions", ui_container=debug_container)

                    # Show a sample position for diagnostics
                    if len(positions) > 0:
                        pos = positions[0]
                        log_debug(f"Example position: {pos.contract.symbol}, {pos.position} @ {pos.avgCost}", ui_container=debug_container)
                else:
                    log_debug("No positions found. If you expect positions, check IB Gateway permissions.", "warning", ui_container=debug_container)
            except Exception as e:
                log_debug(f"Error retrieving positions: {e}", "error", ui_container=debug_container)
                log_debug(traceback.format_exc(), "error", display_ui=False)
            
            log_debug("Connection process completed", ui_container=debug_container)
            return True
        except Exception as e:
            log_debug(f"Unhandled exception during connection: {e}", "error", ui_container=debug_container)
            log_debug(traceback.format_exc(), "error", display_ui=False)
            return False
    
    log_debug("Already connected to TWS", ui_container=debug_container)
    return True

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
        log_debug("Starting portfolio data retrieval", "info")
        try:
            ib.reqMarketDataType(4)
        except Exception as market_data_type_error:
            log_debug(f"Unable to set delayed-frozen market data type: {market_data_type_error}", "warning")
        
        # Get account summary with timeout
        log_debug("Fetching account data...", "info")
        
        try:
            # Fetch account summary with a timeout
            account_summary = await get_account_summary_compat(ib, timeout=20.0)
            
            if not account_summary:
                log_debug("Account summary is empty", "warning")
                return None, None, None
                
            log_debug(f"Got {len(account_summary)} account values", "info")
            
            account_df = pd.DataFrame([(row.tag, row.value) for row in account_summary], 
                                columns=['Tag', 'Value'])
            account_df = account_df.set_index('Tag')
            
            # Update debug state
            debug_state['account_data_received'] = True
            
        except asyncio.TimeoutError:
            log_debug("Timeout occurred while waiting for account data (20s)", "error")
            return None, None, None
        except Exception as account_error:
            log_debug(f"Error getting account data: {account_error}", "error")
            log_debug(traceback.format_exc(), "error", display_ui=False)
            return None, None, None
        
        # Get positions with timeout
        log_debug("Fetching positions...", "info")
        
        try:
            # Fetch positions with a timeout
            positions = await get_positions_compat(ib, timeout=20.0)
            positions = positions or []
            if not positions:
                log_debug("No positions found", "warning")
            else:
                log_debug(f"Got {len(positions)} positions", "info")
                debug_state['positions_received'] = True
            
        except asyncio.TimeoutError:
            log_debug("Timeout occurred while waiting for position data (20s)", "error")
            positions = []
        except Exception as positions_error:
            log_debug(f"Error getting positions: {positions_error}", "error")
            log_debug(traceback.format_exc(), "error", display_ui=False)
            positions = []
        
        # Create a dictionary to store positions by underlying
        positions_by_underlying = {}
        underlying_price_cache = {}
        
        # Process positions
        log_debug("Processing positions...", "info")
        position_count = 0
        position_errors = 0
        
        # Process each position with individual error handling
        for pos in positions:
            try:
                position_count += 1
                contract = pos.contract
                underlying_symbol = contract.symbol
                
                log_debug(f"Processing position {position_count}/{len(positions)}: {underlying_symbol}", "debug", display_ui=False)
                
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
                        log_debug(f"Error requesting market data for {underlying_symbol}: {ticker_error}", "warning")
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
                            log_debug(f"No market price for {underlying_symbol}, using avg cost: {underlying_price}", "warning")
                        else:
                            underlying_price = 100.0
                            log_debug(f"No price data for {underlying_symbol}, using 100 placeholder", "warning")

                    underlying_price_cache[underlying_symbol] = underlying_price
                
                if position_count <= 5:  # Show debug for first few positions
                    log_debug(f"Position {position_count}: {underlying_symbol} @ {underlying_price}", "debug")
                
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
                        log_debug(f"Error processing option {contract.symbol}: {option_error}", "warning")
                        position_errors += 1
                        continue
            
            except Exception as position_error:
                log_debug(f"Error processing position {position_count}: {position_error}", "warning")
                position_errors += 1
                continue
        
        if position_errors > 0:
            log_debug(f"Encountered errors in {position_errors}/{position_count} positions", "warning")
        
        log_debug("Creating dataframe...", "info")
        
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
                log_debug(f"Error calculating values for {symbol}: {calc_error}", "warning")
                continue
        
        underlying_df = pd.DataFrame(underlying_data)
        log_debug(f"Created dataframe with {len(underlying_df)} rows", "info")
        
        # Calculate portfolio metrics
        log_debug("Calculating metrics...", "info")
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
            
            log_debug("Metrics calculated successfully", "info")
        except Exception as metrics_error:
            log_debug(f"Error calculating metrics: {metrics_error}", "error")
            log_debug(traceback.format_exc(), "error", display_ui=False)
            # Handle case where account data doesn't have the expected fields
            pass
        
        log_debug("Portfolio data retrieval complete", "info")
        return account_df, underlying_df, positions_by_underlying
        
    except Exception as e:
        log_debug(f"Error in portfolio data retrieval: {str(e)}", "error")
        log_debug(traceback.format_exc(), "error", display_ui=False)
        return None, None, None

# Helper function to process option positions
async def process_option_position(ib, contract, pos, underlying_symbol, underlying_price, positions_by_underlying):
    log_debug(f"Processing option: {contract.symbol} {contract.right} {contract.strike}", "debug", display_ui=False)
    
    # Get option data with timeout
    try:
        option_ticker_task = ib.reqMktData(contract)
        await asyncio.sleep(0.2)  # Small delay to respect rate limits
    except Exception as ticker_error:
        log_debug(f"Error requesting option market data: {ticker_error}", "warning")
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

    log_debug(f"Got delta from model Greeks: {delta}", "debug", display_ui=False)

    # Fallback delta calculation when model greeks are unavailable or invalid.
    if delta is None:
        if contract.right == 'C':  # Call option
            delta = 0.7 if underlying_price > contract.strike else 0.3
        else:  # Put option
            delta = -0.7 if underlying_price < contract.strike else -0.3
        log_debug(f"Using fallback delta: {delta}", "debug", display_ui=False)
    
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

    log_debug(f"Option processed: notional={option_notional}, value={option_value}", "debug", display_ui=False)

@time_operation("Portfolio Data Retrieval")
def get_portfolio_data_sync(ib):
    """
    Main synchronous portfolio data path.
    Keeps IB requests on one thread/loop context to avoid nested event-loop errors.
    """
    log_debug("Starting portfolio data retrieval", "info")
    try:
        started_at = time.time()
        try:
            ib.reqMarketDataType(4)
        except Exception as market_data_type_error:
            log_debug(f"Unable to set delayed-frozen market data type: {market_data_type_error}", "warning")

        log_debug("Fetching account data...", "info")
        t0 = time.time()
        account_summary = ib.accountSummary()
        log_debug(f"Fetched account data in {time.time() - t0:.2f}s", "debug", display_ui=False)
        if not account_summary:
            log_debug("Account summary is empty", "warning")
            return None, None, None

        account_df = pd.DataFrame([(row.tag, row.value) for row in account_summary], columns=['Tag', 'Value'])
        account_df = account_df.set_index('Tag')
        debug_state['account_data_received'] = True

        log_debug("Fetching positions...", "info")
        t0 = time.time()
        positions = ib.positions() or []
        log_debug(f"Fetched positions in {time.time() - t0:.2f}s", "debug", display_ui=False)
        if positions:
            log_debug(f"Got {len(positions)} positions", "info")
            debug_state['positions_received'] = True
        else:
            log_debug("No positions found", "warning")

        t0 = time.time()
        portfolio_items = ib.portfolio() or []
        log_debug(f"Fetched portfolio items in {time.time() - t0:.2f}s", "debug", display_ui=False)

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

        def cache_underlying_price(symbol, price_value):
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

        def absorb_option_tickers(option_tickers):
            for key, ticker in option_tickers.items():
                delta = option_delta_from_ticker(ticker)
                if is_valid_number(delta):
                    option_delta_map[key] = float(delta)
                    cache_option_delta(key, delta)

                option_price = pick_price_from_ticker(ticker)
                if option_price is not None:
                    option_price_map[key] = option_price

                underlying_symbol = key[0]
                und_price = option_underlying_price_from_ticker(ticker)
                if und_price is not None and underlying_symbol not in underlying_market_price_map:
                    underlying_market_price_map[underlying_symbol] = und_price
                    underlying_price_source[underlying_symbol] = "option_greeks"
                    cache_underlying_price(underlying_symbol, und_price)

        def fetch_missing_underlyings(wait_seconds, chunk_size, label):
            missing_underlyings = [s for s in underlying_symbols if s not in underlying_market_price_map]
            if not missing_underlyings:
                return

            t0 = time.time()
            stock_contracts = [Stock(symbol, 'SMART', 'USD') for symbol in missing_underlyings]
            stock_tickers = gather_stock_market_data(
                ib,
                stock_contracts,
                wait_seconds=wait_seconds,
                chunk_size=chunk_size,
            )
            log_debug(
                f"{label}: requested {len(stock_contracts)} underlying streams in {time.time() - t0:.2f}s",
                "debug",
                display_ui=False,
            )

            for symbol, ticker in stock_tickers.items():
                price = pick_price_from_ticker(ticker)
                if price is not None:
                    underlying_market_price_map[symbol] = price
                    underlying_price_source[symbol] = "snapshot"
                    cache_underlying_price(symbol, price)

        if option_contracts:
            t0 = time.time()
            option_tickers = gather_option_market_data(
                ib,
                option_contracts,
                wait_seconds=0.6,
                chunk_size=6,
            )
            log_debug(
                f"Requested {len(option_contracts)} option streams in {time.time() - t0:.2f}s",
                "debug",
                display_ui=False,
            )
            absorb_option_tickers(option_tickers)

        fetch_missing_underlyings(wait_seconds=0.5, chunk_size=12, label="Initial fetch")

        # If live coverage is poor, do a slower second pass before relying on cache/fallback.
        min_live_quotes = max(5, int(len(underlying_symbols) * 0.5))
        if underlying_symbols and len(underlying_market_price_map) < min_live_quotes:
            log_debug(
                f"Low live quote coverage ({len(underlying_market_price_map)}/{len(underlying_symbols)}); retrying fetch",
                "warning",
                display_ui=False,
            )
            if option_contracts:
                t0 = time.time()
                option_tickers = gather_option_market_data(
                    ib,
                    option_contracts,
                    wait_seconds=1.0,
                    chunk_size=4,
                )
                log_debug(
                    f"Retry fetch: requested {len(option_contracts)} option streams in {time.time() - t0:.2f}s",
                    "debug",
                    display_ui=False,
                )
                absorb_option_tickers(option_tickers)

            fetch_missing_underlyings(wait_seconds=1.0, chunk_size=8, label="Retry fetch")

        for idx, pos in enumerate(positions, start=1):
            try:
                contract = pos.contract
                underlying_symbol = contract.symbol
                log_debug(f"Processing position {idx}/{len(positions)}: {underlying_symbol}", "debug", display_ui=False)

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
                                cache_underlying_price(underlying_symbol, implied_price)

                    if stock_market_value is None:
                        known_market_price = underlying_market_price_map.get(underlying_symbol)
                        if known_market_price is None:
                            known_market_price = underlying_price_cache.get(underlying_symbol)
                            if known_market_price is not None:
                                underlying_price_source[underlying_symbol] = "cached"
                        if is_valid_number(known_market_price) and float(known_market_price) > 0:
                            stock_market_value = float(known_market_price) * pos.position
                            underlying_market_price_map[underlying_symbol] = float(known_market_price)
                            cache_underlying_price(underlying_symbol, known_market_price)

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
                log_debug(f"Error processing position {idx}: {position_error}", "warning")
                position_errors += 1
                continue

        if position_errors > 0:
            log_debug(f"Encountered errors in {position_errors}/{len(positions)} positions", "warning")

        log_debug("Creating dataframe...", "info")
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

                cache_underlying_price(symbol, market_price)
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
                log_debug(f"Error calculating values for {symbol}: {calc_error}", "warning")
                continue

        source_counts = {}
        for row in underlying_data:
            source = row.get('Underlying Price Source', 'unknown')
            source_counts[source] = source_counts.get(source, 0) + 1
        if source_counts:
            log_debug(f"Underlying price sources: {source_counts}", "info", display_ui=False)
            fallback_symbols = [
                row.get('Symbol')
                for row in underlying_data
                if row.get('Underlying Price Source') in ('cost_basis', 'unavailable')
            ]
            if fallback_symbols:
                log_debug(f"Fallback-priced symbols: {fallback_symbols}", "info", display_ui=False)

        underlying_df = pd.DataFrame(underlying_data)
        log_debug(f"Created dataframe with {len(underlying_df)} rows", "info")

        log_debug("Calculating metrics...", "info")
        nlv = get_account_value(account_df, 'NetLiquidation', numeric=True, default=0.0)
        gross_pos_val = get_account_value(account_df, 'GrossPositionValue', numeric=True, default=0.0)
        if not is_valid_number(total_npv):
            total_npv = 0.0

        notional_leverage_ratio = total_npv / nlv if nlv > 0 else 0
        standard_leverage_ratio = gross_pos_val / nlv if nlv > 0 else 0

        account_df.loc['NGAV (Notional Gross Asset Value)', 'Value'] = format_currency(total_npv)
        account_df.loc['NLR (Notional Leverage Ratio)', 'Value'] = f"{notional_leverage_ratio:.2f}"
        account_df.loc['Standard Leverage Ratio', 'Value'] = f"{standard_leverage_ratio:.2f}"

        log_debug("Metrics calculated successfully", "info")
        log_debug(f"Portfolio data retrieval complete in {time.time() - started_at:.2f}s", "info")
        return account_df, underlying_df, positions_by_underlying

    except Exception as e:
        log_debug(f"Error in portfolio data retrieval: {str(e)}", "error")
        log_debug(traceback.format_exc(), "error", display_ui=False)
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
if auto_refresh:
    refresh_interval = st.sidebar.slider("Refresh Interval (seconds)", 5, 60, 10)
    time.sleep(refresh_interval)
    st.rerun()

if st.sidebar.button("Clear Quote Cache"):
    runtime_cache = get_runtime_cache()
    runtime_cache['underlying_prices'].clear()
    runtime_cache['option_deltas'].clear()
    st.session_state.pop('underlying_price_cache', None)
    st.session_state.pop('option_delta_cache', None)
    st.sidebar.success("Cleared quote cache.")
# Diagnostics section
st.sidebar.markdown("---")
st.sidebar.title("Diagnostics")
if st.sidebar.button("Test API Data Access"):
    with st.sidebar.expander("API Test Results", expanded=True):
        st.write("Testing IB API connection...")
        
        if not ib.isConnected():
            st.error("Not connected to IB Gateway")
        else:
            st.success("Connected to IB Gateway")
            
            # Test account data
            try:
                st.write("Requesting account data...")
                account_values = ib.accountSummary()
                st.write(f"Received {len(account_values)} account values")
                
                # Display sample
                if account_values:
                    df = pd.DataFrame([(val.tag, val.value) for val in account_values[:10]], 
                                      columns=['Tag', 'Value'])
                    st.dataframe(df)
                else:
                    st.warning("No account data received")
            except Exception as e:
                st.error(f"Error getting account data: {e}")
                
            # Test positions
            try:
                st.write("Requesting positions...")
                positions = ib.positions()
                st.write(f"Received {len(positions)} positions")
                
                # Display sample
                if positions:
                    pos_data = []
                    for pos in positions[:5]:  # Show first 5 positions
                        pos_data.append({
                            'Symbol': pos.contract.symbol,
                            'SecType': pos.contract.secType,
                            'Position': pos.position,
                            'Avg Cost': pos.avgCost
                        })
                    st.dataframe(pd.DataFrame(pos_data))
                else:
                    st.warning("No positions received")
            except Exception as e:
                st.error(f"Error getting positions: {e}")

if st.sidebar.button("Direct Portfolio Fetch"):
    st.sidebar.info("Directly fetching portfolio data (bypassing threading)...")
    
# Remove obsolete threading functions - Streamlit doesn't work with background threads


# Remove obsolete background update functions - they don't work with Streamlit

# Streamlit-compatible main execution
def main():
    """Main function that runs within Streamlit's execution model"""
    setup_asyncio_event_loop()
    
    log_debug("Starting application", "info")
    
    # Add debug panel to sidebar
    display_debug_panel()
    
    # Check for force reconnect from debug panel
    if 'force_reconnect' in st.session_state and st.session_state.force_reconnect:
        log_debug("Force reconnect requested from debug panel", "info")
        if ib.isConnected():
            try:
                ib.disconnect()
                log_debug("Disconnected for force reconnect", "info")
            except Exception as disconnect_error:
                log_debug(f"Error during disconnect for force reconnect: {disconnect_error}", "warning")
        st.session_state.force_reconnect = False
    
    # Try to connect if not already connected
    if not ib.isConnected():
        log_debug("Attempting connection to TWS", "info")
        try:
            connection_success = connect_to_ib()
        except Exception as conn_error:
            log_debug(f"Unhandled exception in connect_to_ib: {conn_error}", "error")
            log_debug(traceback.format_exc(), "error", display_ui=False)
            connection_success = False
    else:
        connection_success = True

    # Refresh sidebar indicator after connection logic runs in this same rerun.
    if ib.isConnected():
        connection_status.success("Connected to TWS")
    else:
        connection_status.error("Not connected to TWS")
    
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
                            log_debug(f"Error updating metrics: {e}", "error")
                    except Exception as container_error:
                        log_debug(f"Error with metrics container: {container_error}", "error")
                
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
                        log_debug(f"Error updating positions table: {table_error}", "error")
        except Exception as update_error:
            log_debug(f"Error updating portfolio data: {update_error}", "error")
            
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
                log_debug(f"Error updating options data: {options_error}", "error")
    else:
        st.error("Failed to connect to Interactive Brokers. Please make sure TWS or IB Gateway is running.")

# Add a new advanced debug section at the end of the app
def add_advanced_debug_section():
    st.sidebar.markdown("---")
    with st.sidebar.expander("Advanced Debugging", expanded=False):
        st.subheader("Connection Testing")
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Test TWS Connection"):
                log_debug("Manual connection test initiated", "info")
                if connect_to_ib():
                    st.success("Connection test successful")
                else:
                    st.error("Connection test failed")
        
        with col2:
            if st.button("Direct Account Data Test"):
                log_debug("Direct account data test initiated", "info")
                if not ib.isConnected():
                    st.error("Not connected to TWS")
                else:
                    try:
                        account_values = ib.accountSummary()
                        if account_values:
                            st.success(f"Received {len(account_values)} account values directly")
                            # Show first few items
                            for i, val in enumerate(account_values[:5]):
                                st.text(f"{val.tag}: {val.value}")
                        else:
                            st.warning("No account data received in direct test")
                    except Exception as e:
                        st.error(f"Error in direct account test: {e}")
        
        st.subheader("Connection Diagnostics")
        
        if st.button("Run Diagnostics"):
            log_debug("Running connection diagnostics", "info")
            diagnostics = {}
            
            # Check if TWS is running
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(1)
                result = s.connect_ex(('127.0.0.1', 7497))
                s.close()
                
                if result == 0:
                    diagnostics["TWS Port"] = "Open (7497)"
                else:
                    diagnostics["TWS Port"] = "Closed or blocked"
            except Exception as e:
                diagnostics["TWS Port Check Error"] = str(e)
            
            # Check API settings if connected
            if ib.isConnected():
                diagnostics["API Connection"] = "Active"
                diagnostics["Client ID"] = ib.client.clientId
                diagnostics["Server Version"] = ib.client.serverVersion()
                
                # Try to get managed accounts
                try:
                    accounts = ib.client.getAccounts()
                    diagnostics["Available Accounts"] = accounts
                except Exception as e:
                    diagnostics["Account Error"] = str(e)
                
                # Check if we can do a basic API call
                try:
                    time_now = ib.reqCurrentTime()
                    diagnostics["Server Time"] = str(time_now)
                except Exception as e:
                    diagnostics["Time Request Error"] = str(e)
            else:
                diagnostics["API Connection"] = "Inactive"
            
            st.json(diagnostics)
        
        # Add log level control
        st.subheader("Logging Level")
        log_level = st.selectbox(
            "Set Logging Level",
            ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            index=1  # Default to INFO
        )
        
        if st.button("Apply Log Level"):
            logger.setLevel(getattr(logging, log_level))
            st.success(f"Logging level set to {log_level}")
            log_debug(f"Logging level changed to {log_level}", "info")

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
    
    # Add advanced debug section
    add_advanced_debug_section()
    
    # Run the main function
    main()
