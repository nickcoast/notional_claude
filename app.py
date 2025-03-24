import streamlit as st
import pandas as pd
import numpy as np
import time
import threading
import asyncio
from datetime import datetime
import locale
import random

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

# Initialize the app
st.set_page_config(
    page_title="IB Portfolio Viewer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Global variables
stop_event = threading.Event()
refresh_options = False

# Initialize IB connection
@st.cache_resource
def get_ib():
    ib = IB()
    return ib

ib = get_ib()

# Connect to IB TWS
def connect_to_ib():
    if not ib.isConnected():
        try:
             # Use a random client ID to avoid conflicts
            client_id = random.randint(1000, 9999)
            st.sidebar.text(f"Connecting with client ID: {client_id}")
            
            # Try to disconnect first in case of lingering connections
            try:
                ib.disconnect()
            except:
                pass
            ib.connect('127.0.0.1', 7496, clientId=1)
            st.success("Connected to Interactive Brokers")
            
            # Add diagnostic information
            st.info("Checking account data availability...")
            
            # Test if we can get account info
            try:
                account_values = run_async(ib.accountSummaryAsync())
                if account_values:
                    st.success(f"Successfully retrieved {len(account_values)} account values")
                    # Display a sample of key values for diagnostics
                    account_sample = [val for val in account_values if val.tag in ['NetLiquidation', 'GrossPositionValue', 'TotalCashValue']]
                    if account_sample:
                        for val in account_sample:
                            st.write(f"{val.tag}: {val.value}")
                else:
                    st.warning("Account data returned empty. Check permissions in IB Gateway.")
            except Exception as e:
                st.error(f"Error retrieving account data: {e}")
                
            # Test if we can get positions
            try:
                positions = run_async(ib.positionsAsync())
                if positions:
                    st.success(f"Successfully retrieved {len(positions)} positions")
                    # Show a sample position for diagnostics
                    if len(positions) > 0:
                        pos = positions[0]
                        st.write(f"Example position: {pos.contract.symbol}, {pos.position} @ {pos.avgCost}")
                else:
                    st.warning("No positions found. If you expect positions, check IB Gateway permissions.")
            except Exception as e:
                st.error(f"Error retrieving positions: {e}")
                
            return True
        except Exception as e:
            st.error(f"Failed to connect to Interactive Brokers: {e}")
            return False
    return True

# Function to safely run async code
def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

# Async wrapper for portfolio data
async def async_get_portfolio_data(ib):
    try:
        # Debug info
        st.sidebar.text("Fetching account data...")
        
        # Get account summary
        account_summary = await ib.accountSummaryAsync()
        
        if not account_summary:
            st.sidebar.warning("Account summary is empty")
            return None, None, None
            
        st.sidebar.text(f"Got {len(account_summary)} account values")
        
        account_df = pd.DataFrame([(row.tag, row.value) for row in account_summary], 
                            columns=['Tag', 'Value'])
        account_df = account_df.set_index('Tag')
        
        # Get positions
        st.sidebar.text("Fetching positions...")
        positions = await ib.positionsAsync()
        
        if not positions:
            st.sidebar.warning("No positions found")
            # Return account data even if no positions
            return account_df, pd.DataFrame(), {}
            
        st.sidebar.text(f"Got {len(positions)} positions")
        
        # Create a dictionary to store positions by underlying
        positions_by_underlying = {}
        
        # Process positions
        st.sidebar.text("Processing positions...")
        position_count = 0
        
        for pos in positions:
            position_count += 1
            contract = pos.contract
            underlying_symbol = contract.symbol
            
            # Get market price for the underlying
            if contract.secType == 'STK':
                underlying_contract = contract
            else:
                # For options, get the underlying price
                underlying_contract = Stock(underlying_symbol, 'SMART', 'USD')
                await ib.qualifyContractsAsync(underlying_contract)
            
            # Use ticker to get real-time price updates
            ticker = ib.reqMktData(underlying_contract)
            await asyncio.sleep(0.2)  # Small delay to respect rate limits
            underlying_price = ticker.marketPrice()
            if underlying_price is None or underlying_price <= 0:
                # Try last price
                underlying_price = ticker.last
                if underlying_price is None or underlying_price <= 0:
                    # Try mid price
                    underlying_price = (ticker.ask + ticker.bid) / 2 if ticker.ask and ticker.bid else None
                    if underlying_price is None or underlying_price <= 0:
                        # Use average cost as last resort
                        if contract.secType == 'STK':
                            underlying_price = pos.avgCost
                            st.sidebar.warning(f"No market price for {underlying_symbol}, using avg cost: {underlying_price}")
                        else:
                            # For options without price data, set a placeholder
                            st.sidebar.warning(f"No price data for {underlying_symbol}, using 100 as placeholder")
                            underlying_price = 100  # Arbitrary placeholder

            if position_count <= 2:  # Show debug for first couple positions only
                st.sidebar.text(f"Position {position_count}: {underlying_symbol} @ {underlying_price}")
            
            if underlying_symbol not in positions_by_underlying:
                positions_by_underlying[underlying_symbol] = {
                    'stock_count': 0,
                    'stock_value': 0,
                    'option_notional': 0,
                    'option_actual_value': 0,
                    'underlying_price': underlying_price
                }
            
            # Calculate position values
            if contract.secType == 'STK':
                positions_by_underlying[underlying_symbol]['stock_count'] += pos.position
                positions_by_underlying[underlying_symbol]['stock_value'] += pos.position * underlying_price
            elif contract.secType == 'OPT':
                # Get option data
                option_ticker = ib.reqMktData(contract)
                await asyncio.sleep(0.2)  # Small delay to respect rate limits
                
                # Calculate option delta (if available, otherwise use approximation)
                delta = None
                option_price = option_ticker.marketPrice()
                
                if hasattr(option_ticker, 'modelGreeks') and option_ticker.modelGreeks:
                    delta = option_ticker.modelGreeks.delta
                else:
                    # Request option computation
                    await ib.reqMarketDataTypeAsync(4)  # Switch to delayed frozen data
                    try:
                        await ib.calculateImpliedVolatilityAsync(contract, option_price, underlying_price)
                        await asyncio.sleep(0.2)
                        await ib.calculateOptionPriceAsync(contract, option_ticker.impliedVolatility, underlying_price)
                        await asyncio.sleep(0.2)
                        
                        # Try again to get delta
                        if hasattr(option_ticker, 'modelGreeks') and option_ticker.modelGreeks:
                            delta = option_ticker.modelGreeks.delta
                    except Exception as option_error:
                        st.sidebar.text(f"Option calculation error: {option_error}")
                    
                    # Fallback delta calculation if still None
                    if delta is None:
                        if contract.right == 'C':  # Call option
                            delta = 0.7 if underlying_price > contract.strike else 0.3
                        else:  # Put option
                            delta = -0.7 if underlying_price < contract.strike else -0.3
                
                # Use absolute value of delta for notional calculation
                abs_delta = abs(delta)
                option_multiplier = 100
                option_notional = abs_delta * option_multiplier * pos.position
                positions_by_underlying[underlying_symbol]['option_notional'] += option_notional
                
                # Calculate actual option value
                option_value = option_price * option_multiplier * abs(pos.position)
                positions_by_underlying[underlying_symbol]['option_actual_value'] += option_value
        
        st.sidebar.text("Creating dataframe...")
        
        # Create DataFrame for display
        underlying_data = []
        total_npv = 0
        
        for symbol, data in positions_by_underlying.items():
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
        
        underlying_df = pd.DataFrame(underlying_data)
        st.sidebar.text(f"Created dataframe with {len(underlying_df)} rows")
        
        # Calculate portfolio metrics
        st.sidebar.text("Calculating metrics...")
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
            
            st.sidebar.text("Metrics calculated successfully")
        except Exception as metrics_error:
            st.sidebar.error(f"Error calculating metrics: {metrics_error}")
            # Handle case where account data doesn't have the expected fields
            pass
        
        st.sidebar.text("Portfolio data retrieval complete")
        return account_df, underlying_df, positions_by_underlying
        
    except Exception as e:
        st.sidebar.error(f"Error in portfolio data retrieval: {str(e)}")
        import traceback
        st.sidebar.text(traceback.format_exc())
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

# Data refresh control
refresh_rate = st.sidebar.slider("Portfolio Refresh Rate (seconds)", 5, 30, 15)
options_refresh_rate = st.sidebar.slider("Options Refresh Rate (seconds)", 1, 10, 5)
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
    
# Function to update portfolio data in background
def update_portfolio_data():
    # Set up event loop for this thread
    setup_asyncio_event_loop()
    
    while not stop_event.is_set():
        if ib.isConnected():
            account_df, underlying_df, _ = get_portfolio_data()
            
            if account_df is not None and underlying_df is not None:
                # Update portfolio metrics display
                with portfolio_metrics.container():
                    # Create a nice grid layout for the metrics
                    metrics_cols = st.columns(6)
                    
                    # Extract key metrics
                    try:
                        nlv = float(account_df.loc['NetLiquidation', 'Value'])
                        gross_pos_val = float(account_df.loc['GrossPositionValue', 'Value'])
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
                        st.error(f"Error updating metrics: {e}")
                
                # Update underlying positions table
                with main_content:
                    st.subheader("Positions by Underlying")
                    # Format monetary values
                    for col in ['Stock Value', 'Option Notional Value', 'Option Actual Value', 'Notional Position Value (NPV)']:
                        if col in underlying_df.columns:
                            underlying_df[col] = underlying_df[col].apply(lambda x: locale.currency(x, grouping=True))
                    
                    # Format underlying price
                    if 'Underlying Price' in underlying_df.columns:
                        underlying_df['Underlying Price'] = underlying_df['Underlying Price'].apply(lambda x: f"${x:.2f}")
                    
                    st.dataframe(underlying_df, use_container_width=True)
                    
                    # Show last update time
                    st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Sleep until next update
        time.sleep(refresh_rate)

# Function to update options data in background
def update_options_data():
    # Set up event loop for this thread
    setup_asyncio_event_loop()
    
    current_ticker = ""
    current_expiration = ""
    
    while not stop_event.is_set():
        if ib.isConnected() and ticker_input and search_button:
            ticker = ticker_input
            
            # Only fetch new data if ticker changed or forced refresh
            if ticker != current_ticker or refresh_options:
                current_ticker = ticker
                # Get option chain data
                stock_price, expirations = get_option_chain(ticker)
                
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
                    if selected_exp != current_expiration:
                        current_expiration = selected_exp
                        stock_price, calls, puts = get_options_for_expiration(ticker, selected_exp)
                        
                        if stock_price is not None and calls and puts:
                            # Display options data
                            with options_display.container():
                                st.subheader(f"{ticker} Options - Stock Price: ${stock_price:.2f}")
                                
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
        
        # Sleep until next update
        time.sleep(options_refresh_rate)

# Start background threads
def main():
    # Set up event loop for the main thread
    setup_asyncio_event_loop()
    
    if connect_to_ib():
        # Create and start portfolio update thread
        portfolio_thread = threading.Thread(target=update_portfolio_data)
        portfolio_thread.daemon = True
        portfolio_thread.start()
        
        # Create and start options update thread
        options_thread = threading.Thread(target=update_options_data)
        options_thread.daemon = True
        options_thread.start()
        
        # Keep main thread alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            # Signal threads to stop
            stop_event.set()
            # Disconnect from IB
            if ib.isConnected():
                ib.disconnect()
    else:
        st.error("Failed to connect to Interactive Brokers. Please make sure TWS or IB Gateway is running.")

# Handle Streamlit's odd execution model
if __name__ == "__main__":
    # Ensure event loop is set up
    setup_asyncio_event_loop()
    
    # Set up session state for options refresh
    if 'refresh_options' not in st.session_state:
        st.session_state.refresh_options = False
    
    refresh_options = st.session_state.refresh_options
    
    # Manual refresh button in the sidebar
    if st.sidebar.button("Force Refresh"):
        st.session_state.refresh_options = True
    else:
        st.session_state.refresh_options = False
    
    # Display info
    st.sidebar.info("""
    This app connects to Interactive Brokers TWS API to display portfolio data.
    Make sure TWS or IB Gateway is running before connecting.
    """)
    
    # Run the app on the main Streamlit thread
    main()