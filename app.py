import streamlit as st
import pandas as pd
import numpy as np
from ib_insync import *
import time
import threading
import asyncio
from datetime import datetime, timedelta
import locale

# Set locale for proper currency formatting
locale.setlocale(locale.LC_ALL, '')

# Initialize the app
st.set_page_config(
    page_title="IB Portfolio Viewer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

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
            ib.connect('127.0.0.1', 7497, clientId=1)  # Change port to 7496 for IB Gateway
            st.success("Connected to Interactive Brokers")
            return True
        except Exception as e:
            st.error(f"Failed to connect to Interactive Brokers: {e}")
            return False
    return True

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

# Function to get portfolio data
def get_portfolio_data():
    if not ib.isConnected():
        return None, None, None
    
    # Get account summary
    account_summary = ib.accountSummary()
    account_df = pd.DataFrame([(row.tag, row.value) for row in account_summary], 
                              columns=['Tag', 'Value'])
    account_df = account_df.set_index('Tag')
    
    # Get positions
    positions = ib.positions()
    
    # Create a dictionary to store positions by underlying
    positions_by_underlying = {}
    
    # Process positions
    for pos in positions:
        contract = pos.contract
        underlying_symbol = contract.symbol
        
        # Get market price for the underlying
        if contract.secType == 'STK':
            underlying_price = ib.reqMktData(contract).marketPrice()
            ib.sleep(0.1)  # Small delay to respect rate limits
        else:
            # For options, get the underlying price
            underlying_contract = Stock(underlying_symbol, 'SMART', 'USD')
            ib.qualifyContracts(underlying_contract)
            underlying_price = ib.reqMktData(underlying_contract).marketPrice()
            ib.sleep(0.1)  # Small delay
        
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
            option_data = ib.reqMktData(contract)
            ib.sleep(0.1)  # Small delay
            
            # Calculate option delta (if available, otherwise use 0.5 as placeholder)
            if hasattr(option_data, 'modelGreeks') and option_data.modelGreeks:
                delta = option_data.modelGreeks.delta
            else:
                # Request option computation
                ib.reqMarketDataType(4)  # Switch to delayed frozen data
                ib.calculateImpliedVolatility(contract, option_data.marketPrice(), underlying_price)
                ib.sleep(0.2)
                ib.calculateOptionPrice(contract, option_data.impliedVolatility, underlying_price)
                ib.sleep(0.2)
                
                # Try again to get delta
                if hasattr(option_data, 'modelGreeks') and option_data.modelGreeks:
                    delta = option_data.modelGreeks.delta
                else:
                    # Fallback delta calculation based on in-the-money status
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
            option_price = option_data.marketPrice()
            option_value = option_price * option_multiplier * abs(pos.position)
            positions_by_underlying[underlying_symbol]['option_actual_value'] += option_value
    
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
    
    # Calculate portfolio metrics
    try:
        nlv = float(account_df.loc['NetLiquidation', 'Value'])
        gross_pos_val = float(account_df.loc['GrossPositionValue', 'Value'])
        
        # Calculate notional leverage ratio
        notional_leverage_ratio = total_npv / nlv if nlv > 0 else 0
        standard_leverage_ratio = gross_pos_val / nlv if nlv > 0 else 0
        
        # Add NGAV and NLR to account summary
        account_df.loc['NGAV (Notional Gross Asset Value)', 'Value'] = locale.currency(total_npv, grouping=True)
        account_df.loc['NLR (Notional Leverage Ratio)', 'Value'] = f"{notional_leverage_ratio:.2f}"
        account_df.loc['Standard Leverage Ratio', 'Value'] = f"{standard_leverage_ratio:.2f}"
    except:
        # Handle case where account data doesn't have the expected fields
        pass
    
    return account_df, underlying_df, positions_by_underlying

# Function to get option chain data
def get_option_chain(ticker):
    if not ib.isConnected():
        return None, None
    
    # Get the stock contract
    stock = Stock(ticker, 'SMART', 'USD')
    ib.qualifyContracts(stock)
    
    # Get current stock price
    stock_data = ib.reqMktData(stock)
    ib.sleep(0.2)
    stock_price = stock_data.marketPrice()
    
    # Get the option chains
    chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
    
    # Get all expiration dates
    expirations = []
    for chain in chains:
        if chain.exchange == 'SMART':
            expirations = sorted(chain.expirations)
            break
    
    # Return all data needed
    return stock_price, expirations

# Function to get options data for specific expiration
def get_options_for_expiration(ticker, expiration):
    if not ib.isConnected():
        return None, None, None
    
    # Get the stock contract
    stock = Stock(ticker, 'SMART', 'USD')
    ib.qualifyContracts(stock)
    
    # Get current stock price
    stock_data = ib.reqMktData(stock)
    ib.sleep(0.2)
    stock_price = stock_data.marketPrice()
    
    # Get option chain for selected expiration
    ib.qualifyContracts(stock)
    chains = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, stock.conId)
    
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
        
        ib.qualifyContracts(call_contract, put_contract)
        
        # Request market data for call
        call_data = ib.reqMktData(call_contract)
        ib.sleep(0.1)  # Small delay to respect rate limits
        
        # Request market data for put
        put_data = ib.reqMktData(put_contract)
        ib.sleep(0.1)  # Small delay
        
        # Get greeks for call
        call_price = call_data.marketPrice()
        call_bid = call_data.bid
        call_ask = call_data.ask
        call_last = call_data.last
        
        # Try to get delta and gamma
        call_delta = None
        call_gamma = None
        
        if hasattr(call_data, 'modelGreeks') and call_data.modelGreeks:
            call_delta = call_data.modelGreeks.delta
            call_gamma = call_data.modelGreeks.gamma
        else:
            # Calculate implied volatility and greeks
            try:
                ib.calculateImpliedVolatility(call_contract, call_price, stock_price)
                ib.sleep(0.2)
                ib.calculateOptionPrice(call_contract, call_data.impliedVolatility, stock_price)
                ib.sleep(0.2)
                
                if hasattr(call_data, 'modelGreeks') and call_data.modelGreeks:
                    call_delta = call_data.modelGreeks.delta
                    call_gamma = call_data.modelGreeks.gamma
            except:
                # Fallback delta calculation
                call_delta = 0.7 if stock_price > strike else 0.3
                call_gamma = 0.01  # Default gamma
        
        # Similarly for put
        put_price = put_data.marketPrice()
        put_bid = put_data.bid
        put_ask = put_data.ask
        put_last = put_data.last
        
        put_delta = None
        put_gamma = None
        
        if hasattr(put_data, 'modelGreeks') and put_data.modelGreeks:
            put_delta = put_data.modelGreeks.delta
            put_gamma = put_data.modelGreeks.gamma
        else:
            try:
                ib.calculateImpliedVolatility(put_contract, put_price, stock_price)
                ib.sleep(0.2)
                ib.calculateOptionPrice(put_contract, put_data.impliedVolatility, stock_price)
                ib.sleep(0.2)
                
                if hasattr(put_data, 'modelGreeks') and put_data.modelGreeks:
                    put_delta = put_data.modelGreeks.delta
                    put_gamma = put_data.modelGreeks.gamma
            except:
                # Fallback delta calculation
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

# UI Layout - Main App
st.title("Interactive Brokers Portfolio Viewer")

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

# Create stop event for background threads
stop_event = threading.Event()

# Function to update portfolio data in background
def update_portfolio_data():
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
                        ngav = locale.atof(account_df.loc['NGAV (Notional Gross Asset Value)', 'Value'].replace(locale.localeconv()['currency_symbol'], ''))
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
