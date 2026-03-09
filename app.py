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
            if 'force_reconnect' not in st.session_state:
                st.session_state.force_reconnect = True
"""
END: Debug helper functions for Interactive Brokers API integration
"""

# Set the event loop policy first
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

# Create a new event loop and set it as the current loop
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Now that we have an event loop, apply nest_asyncio
import nest_asyncio
nest_asyncio.apply()

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
            return float(clean_str)
        except ValueError:
            st.sidebar.warning(f"Could not convert '{value_str}' to float")
            return 0.0
    
    # Already a number
    try:
        return float(value_str)
    except (ValueError, TypeError):
        return 0.0

# Define the helper function for other threads
def setup_asyncio_event_loop():
    """Ensure there is an event loop available for the current thread"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop

# Now import ib_insync after setting up the asyncio environment
from ib_insync import *


# Set locale for proper currency formatting
locale.setlocale(locale.LC_ALL, '')

# Global variables - remove threading elements

# Initialize IB connection
@st.cache_resource
def get_ib():
    ib = IB()
    return ib

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
            except Exception as connect_error:
                log_debug(f"Connection failed: {connect_error}", "error", ui_container=debug_container)
                log_debug(traceback.format_exc(), "error", display_ui=False)
                return False
            
            # Connection succeeded, now check account data
            st.success("Connected to Interactive Brokers")
            
            # Add diagnostic information
            log_debug("Checking account data availability...", ui_container=debug_container)
            
            # Test if we can get account info with timeout handling
            try:
                log_debug("Requesting account summary data (async)...", ui_container=debug_container)
                
                # Use run_async with a timeout - this might be where it's hanging
                async def get_account_with_timeout():
                    try:
                        log_debug("Starting accountSummaryAsync call", ui_container=debug_container)
                        account_values = await ib.accountSummaryAsync()
                        log_debug(f"accountSummaryAsync completed with {len(account_values) if account_values else 0} values", ui_container=debug_container)
                        return account_values
                    except Exception as async_error:
                        log_debug(f"accountSummaryAsync error: {async_error}", "error", ui_container=debug_container)
                        log_debug(traceback.format_exc(), "error", display_ui=False)
                        return None
                
                # Run with a timeout
                log_debug("Running account retrieval with timeout", ui_container=debug_container)
                account_values = run_async(timeout_async(get_account_with_timeout(), timeout=15.0, operation_name="account data retrieval"))
                
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
            except asyncio.TimeoutError:
                log_debug("Timeout occurred while retrieving account data (15s). This may indicate a connection issue with TWS.", "error", ui_container=debug_container)
                # Continue anyway - we might still be able to get positions
            except Exception as e:
                log_debug(f"Error retrieving account data: {e}", "error", ui_container=debug_container)
                log_debug(traceback.format_exc(), "error", display_ui=False)
                # Continue anyway - we might still be able to get positions
                
            # Test if we can get positions with timeout handling
            try:
                log_debug("Requesting position data (async)...", ui_container=debug_container)
                
                # Use run_async with a timeout
                async def get_positions_with_timeout():
                    try:
                        log_debug("Starting positionsAsync call", ui_container=debug_container)
                        positions = await ib.positionsAsync()
                        log_debug(f"positionsAsync completed with {len(positions) if positions else 0} positions", ui_container=debug_container)
                        return positions
                    except Exception as async_error:
                        log_debug(f"positionsAsync error: {async_error}", "error", ui_container=debug_container)
                        log_debug(traceback.format_exc(), "error", display_ui=False)
                        return None
                
                # Run with a timeout
                log_debug("Running position retrieval with timeout", ui_container=debug_container)
                positions = run_async(timeout_async(get_positions_with_timeout(), timeout=15.0, operation_name="position data retrieval"))
                
                if positions:
                    debug_state['positions_received'] = True
                    log_debug(f"Successfully retrieved {len(positions)} positions", ui_container=debug_container)
                    
                    # Show a sample position for diagnostics
                    if len(positions) > 0:
                        pos = positions[0]
                        log_debug(f"Example position: {pos.contract.symbol}, {pos.position} @ {pos.avgCost}", ui_container=debug_container)
                else:
                    log_debug("No positions found. If you expect positions, check IB Gateway permissions.", "warning", ui_container=debug_container)
            except asyncio.TimeoutError:
                log_debug("Timeout occurred while retrieving position data (15s)", "error", ui_container=debug_container)
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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# Async wrapper for portfolio data with improved debugging and timeout handling
@time_operation("Portfolio Data Retrieval")
async def async_get_portfolio_data(ib):
    try:
        # Debug info
        log_debug("Starting portfolio data retrieval", "info")
        
        # Get account summary with timeout
        log_debug("Fetching account data...", "info")
        
        try:
            # Fetch account summary with a timeout
            account_summary_task = asyncio.create_task(ib.accountSummaryAsync())
            account_summary = await asyncio.wait_for(account_summary_task, timeout=10.0)
            
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
            log_debug("Timeout occurred while waiting for account data", "error")
            return None, None, None
        except Exception as account_error:
            log_debug(f"Error getting account data: {account_error}", "error")
            log_debug(traceback.format_exc(), "error", display_ui=False)
            return None, None, None
        
        # Get positions with timeout
        log_debug("Fetching positions...", "info")
        
        try:
            # Fetch positions with a timeout
            positions_task = asyncio.create_task(ib.positionsAsync())
            positions = await asyncio.wait_for(positions_task, timeout=10.0)
            
            if not positions:
                log_debug("No positions found", "warning")
                # Return account data even if no positions
                return account_df, pd.DataFrame(), {}
                
            log_debug(f"Got {len(positions)} positions", "info")
            
            # Update debug state
            debug_state['positions_received'] = True
            
        except asyncio.TimeoutError:
            log_debug("Timeout occurred while waiting for position data", "error")
            # Return account data even if positions timed out
            return account_df, pd.DataFrame(), {}
        except Exception as positions_error:
            log_debug(f"Error getting positions: {positions_error}", "error")
            log_debug(traceback.format_exc(), "error", display_ui=False)
            # Return account data even if positions failed
            return account_df, pd.DataFrame(), {}
        
        # Create a dictionary to store positions by underlying
        positions_by_underlying = {}
        
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
                
                # Get market price for the underlying
                if contract.secType == 'STK':
                    underlying_contract = contract
                else:
                    # For options, get the underlying price
                    underlying_contract = Stock(underlying_symbol, 'SMART', 'USD')
                    try:
                        await asyncio.wait_for(ib.qualifyContractsAsync(underlying_contract), timeout=5.0)
                    except asyncio.TimeoutError:
                        log_debug(f"Timeout qualifying contract for {underlying_symbol}", "warning")
                        # Skip this position
                        position_errors += 1
                        continue
                
                # Use ticker to get real-time price updates
                try:
                    ticker = ib.reqMktData(underlying_contract)
                    await asyncio.sleep(0.2)  # Small delay to respect rate limits
                except Exception as ticker_error:
                    log_debug(f"Error requesting market data for {underlying_symbol}: {ticker_error}", "warning")
                    position_errors += 1
                    continue
                
                underlying_price = ticker.marketPrice()
                
                # Handle missing price data with more detailed logging
                if underlying_price is None or underlying_price <= 0:
                    log_debug(f"No market price for {underlying_symbol}, trying last price", "debug", display_ui=False)
                    
                    # Try last price
                    underlying_price = ticker.last
                    if underlying_price is None or underlying_price <= 0:
                        log_debug(f"No last price for {underlying_symbol}, trying mid price", "debug", display_ui=False)
                        
                        # Try mid price
                        underlying_price = (ticker.ask + ticker.bid) / 2 if ticker.ask and ticker.bid else None
                        if underlying_price is None or underlying_price <= 0:
                            log_debug(f"No mid price for {underlying_symbol}, falling back to alternatives", "debug", display_ui=False)
                            
                            # Use average cost as last resort
                            if contract.secType == 'STK':
                                underlying_price = pos.avgCost
                                log_debug(f"No market price for {underlying_symbol}, using avg cost: {underlying_price}", "warning")
                            else:
                                # For options without price data, set a placeholder
                                log_debug(f"No price data for {underlying_symbol}, using 100 as placeholder", "warning")
                                underlying_price = 100  # Arbitrary placeholder
                
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
                    'Option Notional (Shares)': data['option_notional'] / 100,  # Convert to contract equivalents
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
            nlv = safe_float_conversion(account_df.loc['NetLiquidation', 'Value'])
            gross_pos_val = safe_float_conversion(account_df.loc['GrossPositionValue', 'Value'])
            
            # Calculate notional leverage ratio
            notional_leverage_ratio = total_npv / nlv if nlv > 0 else 0
            standard_leverage_ratio = gross_pos_val / nlv if nlv > 0 else 0
            
            # Add NGAV and NLR to account summary
            account_df.loc['NGAV (Notional Gross Asset Value)', 'Value'] = locale.currency(total_npv, grouping=True)
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
    option_price = option_ticker_task.marketPrice()
    
    if hasattr(option_ticker_task, 'modelGreeks') and option_ticker_task.modelGreeks:
        delta = option_ticker_task.modelGreeks.delta
        log_debug(f"Got delta from model Greeks: {delta}", "debug", display_ui=False)
    else:
        log_debug("No model Greeks available, trying to calculate", "debug", display_ui=False)
        
        # Request option computation with timeout
        try:
            await asyncio.wait_for(ib.reqMarketDataTypeAsync(4), timeout=2.0)  # Switch to delayed frozen data
            
            try:
                await asyncio.wait_for(
                    ib.calculateImpliedVolatilityAsync(contract, option_price, underlying_price),
                    timeout=2.0
                )
                await asyncio.sleep(0.2)
                
                await asyncio.wait_for(
                    ib.calculateOptionPriceAsync(contract, option_ticker_task.impliedVolatility, underlying_price),
                    timeout=2.0
                )
                await asyncio.sleep(0.2)
                
                # Try again to get delta
                if hasattr(option_ticker_task, 'modelGreeks') and option_ticker_task.modelGreeks:
                    delta = option_ticker_task.modelGreeks.delta
                    log_debug(f"Got delta after calculation: {delta}", "debug", display_ui=False)
            except Exception as calc_error:
                log_debug(f"Option calculation error: {calc_error}", "debug", display_ui=False)
        except asyncio.TimeoutError:
            log_debug("Timeout during option calculations", "debug", display_ui=False)
        
        # Fallback delta calculation if still None
        if delta is None:
            if contract.right == 'C':  # Call option
                delta = 0.7 if underlying_price > contract.strike else 0.3
            else:  # Put option
                delta = -0.7 if underlying_price < contract.strike else -0.3
            log_debug(f"Using fallback delta: {delta}", "debug", display_ui=False)
    
    # Use absolute value of delta for notional calculation
    abs_delta = abs(delta)
    option_multiplier = 100
    option_notional = abs_delta * option_multiplier * pos.position
    positions_by_underlying[underlying_symbol]['option_notional'] += option_notional
    
    # Calculate actual option value
    option_value = option_price * option_multiplier * abs(pos.position)
    positions_by_underlying[underlying_symbol]['option_actual_value'] += option_value
    
    log_debug(f"Option processed: notional={option_notional}, value={option_value}", "debug", display_ui=False)

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
        return run_async(async_get_portfolio_data(ib))
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
                account_values = run_async(ib.accountSummaryAsync())
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
                positions = run_async(ib.positionsAsync())
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
            except:
                log_debug("Error during disconnect for force reconnect", "warning")
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
                            nlv = safe_float_conversion(account_df.loc['NetLiquidation', 'Value'])
                            gross_pos_val = safe_float_conversion(account_df.loc['GrossPositionValue', 'Value'])
                            ngav = safe_float_conversion(account_df.loc['NGAV (Notional Gross Asset Value)', 'Value'])
                            nlr = float(account_df.loc['NLR (Notional Leverage Ratio)', 'Value'])
                            std_leverage = float(account_df.loc['Standard Leverage Ratio', 'Value'])
                            
                            metrics_cols[0].metric("Net Liquidation Value", 
                                                 locale.currency(nlv, grouping=True))
                            metrics_cols[1].metric("Gross Position Value", 
                                                 locale.currency(gross_pos_val, grouping=True))
                            metrics_cols[2].metric("NGAV", 
                                                 locale.currency(ngav, grouping=True))
                            metrics_cols[3].metric("Standard Leverage", 
                                                 f"{std_leverage:.2f}x")
                            metrics_cols[4].metric("Notional Leverage Ratio", 
                                                 f"{nlr:.2f}x")
                            metrics_cols[5].metric("Buying Power", 
                                                 account_df.loc['BuyingPower', 'Value'] 
                                                 if 'BuyingPower' in account_df.index else "N/A")
                        except Exception as e:
                            log_debug(f"Error updating metrics: {e}", "error")
                    except Exception as container_error:
                        log_debug(f"Error with metrics container: {container_error}", "error")
                
                # Update underlying positions table
                with main_content:
                    try:
                        st.subheader("Positions by Underlying")
                        # Format monetary values
                        display_df = underlying_df.copy()
                        for col in ['Stock Value', 'Option Notional Value', 'Option Actual Value', 'Notional Position Value (NPV)']:
                            if col in display_df.columns:
                                display_df[col] = display_df[col].apply(lambda x: locale.currency(x, grouping=True))
                        
                        # Format underlying price
                        if 'Underlying Price' in display_df.columns:
                            display_df['Underlying Price'] = display_df['Underlying Price'].apply(lambda x: f"${x:.2f}")
                        
                        st.dataframe(display_df, use_container_width=True)
                        
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
                        account_values = run_async(ib.accountSummaryAsync())
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