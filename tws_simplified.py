"""
TWS Portfolio Viewer (Fixed version)
This version properly sets up the event loop before importing ib_insync
"""
import streamlit as st
import pandas as pd
import numpy as np
import time
import locale
import random
from datetime import datetime
import asyncio
import nest_asyncio
import sys
import os

# Set locale for proper currency formatting
locale.setlocale(locale.LC_ALL, '')

# Fix asyncio event loop for Streamlit
# First set the event loop policy
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

# Create new event loop and set as default
try:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
except Exception as e:
    st.error(f"Error setting up event loop: {e}")

# Apply nest_asyncio to allow nested use of asyncio event loops
try:
    nest_asyncio.apply()
except Exception as e:
    st.error(f"Error applying nest_asyncio: {e}")

# Define helper function for ensuring event loop
def setup_asyncio_event_loop():
    """Ensure there is an event loop available for the current thread"""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop

# Set up event loop before importing ib_insync
setup_asyncio_event_loop()

# Now import ib_insync
try:
    from ib_insync import *
    # Fixed: Remove the qtapp parameter as it's not supported in your version
    util.useQt()  # This will help with Qt integration if needed
except Exception as e:
    st.error(f"Error importing ib_insync: {e}")
    st.stop()

# Initialize the app
st.set_page_config(
    page_title="TWS Portfolio Viewer",
    page_icon="📊",
    layout="wide"
)

# Helper function for safe float conversion
def safe_float_conversion(value_str):
    if value_str is None:
        return 0.0
    
    if isinstance(value_str, str):
        clean_str = value_str.replace(locale.localeconv()['currency_symbol'], '')
        clean_str = clean_str.replace(',', '')
        try:
            return float(clean_str)
        except ValueError:
            return 0.0
    
    try:
        return float(value_str)
    except (ValueError, TypeError):
        return 0.0

# Initialize IB connection
@st.cache_resource
def get_ib():
    ib = IB()
    return ib

ib = get_ib()

# Connect to TWS
def connect_to_tws():
    if not ib.isConnected():
        try:
            # Use a random client ID to avoid conflicts
            client_id = random.randint(1000, 9999)
            st.info(f"Connecting to TWS with client ID: {client_id}")
            
            # Try to disconnect first in case of lingering connections
            try:
                ib.disconnect()
            except:
                pass
                
            # Connect with TWS port (7497)
            ib.connect('127.0.0.1', 7497, clientId=client_id)
            
            # Request delayed market data
            ib.reqMarketDataType(3)  # 3 = delayed data
            
            st.success(f"Connected to TWS with client ID {client_id}")
            return True
        except Exception as e:
            st.error(f"Failed to connect to TWS: {e}")
            return False
    return True

# Get portfolio data (synchronous version)
def get_portfolio_data():
    if not ib.isConnected():
        st.error("Not connected to TWS")
        return None, None, None
    
    try:
        st.info("Requesting account summary...")
        account_summary = ib.accountSummary()
        
        if not account_summary:
            st.warning("Account summary is empty")
            return None, None, None
            
        st.success(f"Got {len(account_summary)} account values")
        
        account_df = pd.DataFrame([(row.tag, row.value) for row in account_summary], 
                            columns=['Tag', 'Value'])
        account_df = account_df.set_index('Tag')
        
        st.info("Requesting positions...")
        positions = ib.positions()
        
        if not positions:
            st.warning("No positions found")
            return account_df, pd.DataFrame(), {}
            
        st.success(f"Got {len(positions)} positions")
        
        # Create a dictionary to store positions by underlying
        positions_by_underlying = {}
        
        # Process positions
        st.info("Processing positions...")
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
                ib.qualifyContracts(underlying_contract)
            
            # Use ticker to get real-time price updates
            ticker = ib.reqMktData(underlying_contract)
            time.sleep(0.2)  # Small delay to respect rate limits
            underlying_price = ticker.marketPrice()
            
            # Handle None or 0 prices
            if underlying_price is None or underlying_price <= 0:
                underlying_price = ticker.last
                if underlying_price is None or underlying_price <= 0:
                    underlying_price = (ticker.ask + ticker.bid) / 2 if ticker.ask and ticker.bid else None
                    if underlying_price is None or underlying_price <= 0:
                        if contract.secType == 'STK':
                            underlying_price = pos.avgCost
                            st.warning(f"No market price for {underlying_symbol}, using avg cost: {underlying_price}")
                        else:
                            st.warning(f"No price data for {underlying_symbol}, using 100 as placeholder")
                            underlying_price = 100  # Arbitrary placeholder
            
            st.text(f"Position {position_count}: {underlying_symbol} @ {underlying_price}")
            
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
                time.sleep(0.2)  # Small delay to respect rate limits
                
                # Calculate option delta (if available, otherwise use approximation)
                delta = None
                option_price = option_ticker.marketPrice()
                
                if hasattr(option_ticker, 'modelGreeks') and option_ticker.modelGreeks:
                    delta = option_ticker.modelGreeks.delta
                else:
                    # Request option computation
                    ib.reqMarketDataType(4)  # Switch to delayed frozen data
                    try:
                        ib.calculateImpliedVolatility(contract, option_price, underlying_price)
                        time.sleep(0.2)
                        ib.calculateOptionPrice(contract, option_ticker.impliedVolatility, underlying_price)
                        time.sleep(0.2)
                        
                        # Try again to get delta
                        if hasattr(option_ticker, 'modelGreeks') and option_ticker.modelGreeks:
                            delta = option_ticker.modelGreeks.delta
                    except Exception as option_error:
                        st.warning(f"Option calculation error: {option_error}")
                    
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
        
        st.info("Creating dataframe...")
        
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
        st.success(f"Created dataframe with {len(underlying_df)} rows")
        
        # Calculate portfolio metrics
        st.info("Calculating metrics...")
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
            
            st.success("Metrics calculated successfully")
        except Exception as metrics_error:
            st.error(f"Error calculating metrics: {metrics_error}")
            # Handle case where account data doesn't have the expected fields
            pass
        
        st.success("Portfolio data retrieval complete")
        return account_df, underlying_df, positions_by_underlying
        
    except Exception as e:
        st.error(f"Error in portfolio data retrieval: {str(e)}")
        import traceback
        st.text(traceback.format_exc())
        return None, None, None

# Get option chain for a ticker
def get_option_chain(ticker):
    if not ib.isConnected():
        st.error("Not connected to TWS")
        return None, None
    
    try:
        # Get the stock contract
        stock = Stock(ticker, 'SMART', 'USD')
        ib.qualifyContracts(stock)
        
        # Get current stock price
        ticker_data = ib.reqMktData(stock)
        time.sleep(0.5)  # Give it a bit more time
        stock_price = ticker_data.marketPrice()
        
        # Get the option chains
        chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
        
        # Get all expiration dates
        expirations = []
        for chain in chains:
            if chain.exchange == 'SMART':
                expirations = sorted(chain.expirations)
                break
        
        return stock_price, expirations
    except Exception as e:
        st.error(f"Error getting option chain: {e}")
        return None, None

# Get options for a specific expiration
def get_options_for_expiration(ticker, expiration):
    if not ib.isConnected():
        st.error("Not connected to TWS")
        return None, None, None
    
    try:
        # Get the stock contract
        stock = Stock(ticker, 'SMART', 'USD')
        ib.qualifyContracts(stock)
        
        # Get current stock price
        ticker_data = ib.reqMktData(stock)
        time.sleep(0.5)
        stock_price = ticker_data.marketPrice()
        
        # Get option chain for selected expiration
        chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
        
        # Find the SMART exchange chain
        chain = next((c for c in chains if c.exchange == 'SMART'), None)
        if not chain:
            st.warning("No SMART exchange option chain found")
            return None, None, None
        
        # Get all strike prices
        strikes = sorted(chain.strikes)
        
        # Create call and put options
        calls = []
        puts = []
        
        # Limit the number of strikes to avoid overloading
        max_strikes = 10  # Adjust this as needed
        middle_index = next((i for i, s in enumerate(strikes) if s >= stock_price), len(strikes)//2)
        start_index = max(0, middle_index - max_strikes//2)
        end_index = min(len(strikes), start_index + max_strikes)
        
        limited_strikes = strikes[start_index:end_index]
        st.info(f"Fetching data for {len(limited_strikes)} strikes around current price")
        
        # Request data for each strike
        for strike in limited_strikes:
            call_contract = Option(ticker, expiration, strike, 'C', 'SMART')
            put_contract = Option(ticker, expiration, strike, 'P', 'SMART')
            
            ib.qualifyContracts(call_contract, put_contract)
            
            # Request market data for call
            call_ticker = ib.reqMktData(call_contract)
            time.sleep(0.1)  # Small delay to respect rate limits
            
            # Request market data for put
            put_ticker = ib.reqMktData(put_contract)
            time.sleep(0.1)  # Small delay
            
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
            call_pct = (call_price / stock_price) * 100 if stock_price and stock_price > 0 else 0
            put_pct = (put_price / stock_price) * 100 if stock_price and stock_price > 0 else 0
            
            # Calculate difference from stock price
            call_diff = call_price - (stock_price - strike) if stock_price and stock_price > strike else call_price
            put_diff = put_price - (strike - stock_price) if stock_price and stock_price < strike else put_price
            
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
    except Exception as e:
        st.error(f"Error getting options data: {e}")
        return None, None, None

# UI Layout
st.title("TWS Portfolio Viewer")

# Connection status and control
st.sidebar.title("TWS Connection Status")
connection_status = st.sidebar.empty()

if not ib.isConnected():
    if st.sidebar.button("Connect to TWS"):
        connect_to_tws()

# Update connection status
if ib.isConnected():
    connection_status.success("Connected to TWS")
else:
    connection_status.error("Not connected to TWS")

# Main content area
tab1, tab2 = st.tabs(["Portfolio", "Options"])

with tab1:
    # Portfolio section
    st.header("Portfolio Data")
    
    # Fetch button for portfolio data
    if st.button("Fetch Portfolio Data"):
        account_df, underlying_df, _ = get_portfolio_data()
        
        if account_df is not None:
            # Display account summary
            st.subheader("Account Summary")
            st.dataframe(account_df)
            
            # Try to display key metrics
            try:
                metrics_cols = st.columns(3)
                nlv = safe_float_conversion(account_df.loc['NetLiquidation', 'Value'])
                gross_pos_val = safe_float_conversion(account_df.loc['GrossPositionValue', 'Value'])
                
                metrics_cols[0].metric("Net Liquidation Value", locale.currency(nlv, grouping=True))
                metrics_cols[1].metric("Gross Position Value", locale.currency(gross_pos_val, grouping=True))
                
                if 'NGAV (Notional Gross Asset Value)' in account_df.index:
                    ngav = safe_float_conversion(account_df.loc['NGAV (Notional Gross Asset Value)', 'Value'])
                    metrics_cols[2].metric("NGAV", locale.currency(ngav, grouping=True))
                
                if 'NLR (Notional Leverage Ratio)' in account_df.index:
                    nlr = safe_float_conversion(account_df.loc['NLR (Notional Leverage Ratio)', 'Value'])
                    st.metric("Notional Leverage Ratio", f"{nlr:.2f}x")
            except Exception as e:
                st.warning(f"Could not display metrics: {e}")
        
        if underlying_df is not None and not underlying_df.empty:
            # Display positions by underlying
            st.subheader("Positions by Underlying")
            
            # Format monetary values
            for col in ['Stock Value', 'Option Notional Value', 'Option Actual Value', 'Notional Position Value (NPV)']:
                if col in underlying_df.columns:
                    underlying_df[col] = underlying_df[col].apply(lambda x: locale.currency(x, grouping=True))
            
            # Format underlying price
            if 'Underlying Price' in underlying_df.columns:
                underlying_df['Underlying Price'] = underlying_df['Underlying Price'].apply(lambda x: f"${x:.2f}")
            
            st.dataframe(underlying_df, use_container_width=True)
        elif underlying_df is not None:
            st.info("No position data found")

# Options Browser tab
with tab2:
    st.header("Options Browser")
    
    # Ticker input
    ticker_input = st.text_input("Enter Ticker Symbol", "")
    
    if ticker_input:
        # Fetch options chain button
        if st.button("Fetch Options Chain"):
            st.info(f"Fetching options data for {ticker_input}...")
            stock_price, expirations = get_option_chain(ticker_input)
            
            if stock_price is not None and expirations:
                st.success(f"Got options chain for {ticker_input} - Current price: ${stock_price:.2f}")
                
                # Format expirations for display
                exp_dates = [datetime.strptime(exp, '%Y%m%d').strftime('%Y-%m-%d') for exp in expirations]
                
                # Show expiration selection
                selected_exp_index = st.select_slider(
                    "Select Expiration Date",
                    options=range(len(exp_dates)),
                    format_func=lambda i: exp_dates[i]
                )
                selected_exp = expirations[selected_exp_index]
                
                # Fetch options for selected expiration
                if st.button(f"Fetch {exp_dates[selected_exp_index]} Options"):
                    stock_price, calls, puts = get_options_for_expiration(ticker_input, selected_exp)
                    
                    if stock_price is not None and calls and puts:
                        # Display options data
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
                    else:
                        st.error("Failed to fetch options data for this expiration")
            else:
                st.error("Failed to fetch options chain")

# Footer with info
st.sidebar.markdown("---")
st.sidebar.info("""
## TWS Settings
1. In TWS, go to Edit > Global Configuration > API > Settings
2. Make sure "Enable ActiveX and Socket Clients" is checked
3. Confirm socket port is 7497
4. Set API precautions to at least "Warning" or "Bypass"
""")

# Disconnect button
if ib.isConnected() and st.sidebar.button("Disconnect"):
    ib.disconnect()
    st.sidebar.success("Disconnected from TWS")
    st.experimental_rerun()

# Proper cleanup on exit
import atexit

def cleanup():
    """Clean up resources before exit"""
    try:
        if ib.isConnected():
            print("Disconnecting from TWS...")
            ib.disconnect()
        print("Closing event loop...")
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.stop()
        if not loop.is_closed():
            loop.close()
        print("Cleanup complete.")
    except Exception as e:
        print(f"Error during cleanup: {e}")

# Register cleanup function to be called on exit
atexit.register(cleanup)