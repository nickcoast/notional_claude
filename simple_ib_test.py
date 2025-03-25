"""
Simplified Interactive Brokers connection test for Streamlit.
Save this as 'simple_ib_test.py' and run with 'streamlit run simple_ib_test.py'
"""
import streamlit as st
import pandas as pd
import asyncio
import random
import time
from datetime import datetime

# Set up asyncio properly
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Now import ib_insync
from ib_insync import *

# Initialize the app
st.set_page_config(
    page_title="IB Simple Test",
    page_icon="📊",
    layout="wide"
)

st.title("Interactive Brokers Simple Connection Test")

# Basic connection function
def connect_to_ib(client_id=None, readonly=True):
    if client_id is None:
        client_id = random.randint(1000, 9999)
    
    ib = IB()
    try:
        st.info(f"Connecting with client ID: {client_id}...")
        ib.connect('127.0.0.1', 7497, clientId=client_id, readonly=readonly)
        st.success(f"Connected to IB Gateway with client ID {client_id}")
        return ib
    except Exception as e:
        st.error(f"Connection failed: {e}")
        return None

# Get account info synchronously
def get_account_info(ib):
    if not ib or not ib.isConnected():
        st.error("Not connected to IB")
        return None
    
    try:
        st.info("Requesting account summary...")
        account_values = ib.accountSummary()
        
        if account_values:
            st.success(f"Received {len(account_values)} account values")
            df = pd.DataFrame([(val.tag, val.value, val.currency) for val in account_values], 
                              columns=['Tag', 'Value', 'Currency'])
            return df
        else:
            st.warning("No account data received")
            return None
    except Exception as e:
        st.error(f"Error getting account info: {e}")
        return None

# Get positions synchronously
def get_positions(ib):
    if not ib or not ib.isConnected():
        st.error("Not connected to IB")
        return None
    
    try:
        st.info("Requesting positions...")
        positions = ib.positions()
        
        if positions:
            st.success(f"Received {len(positions)} positions")
            pos_data = []
            for pos in positions:
                pos_data.append({
                    'Symbol': pos.contract.symbol,
                    'SecType': pos.contract.secType,
                    'Exchange': pos.contract.exchange,
                    'Position': pos.position,
                    'Avg Cost': pos.avgCost
                })
            return pd.DataFrame(pos_data)
        else:
            st.warning("No positions received")
            return None
    except Exception as e:
        st.error(f"Error getting positions: {e}")
        return None

# UI for testing connection
st.subheader("Connection Settings")
col1, col2 = st.columns(2)
with col1:
    client_id = st.number_input("Client ID", min_value=1, max_value=9999999, value=random.randint(1000, 9999))
with col2:
    readonly = st.checkbox("Read-only mode", value=True)

if st.button("Connect to IB Gateway"):
    ib = connect_to_ib(client_id=client_id, readonly=readonly)
    
    if ib and ib.isConnected():
        st.session_state.ib = ib
        
        # Display server info
        st.subheader("Server Information")
        st.write(f"Server Version: {ib.client.serverVersion()}")
        server_time = ib.reqCurrentTime()
        st.write(f"Connection Time: {server_time}")
        
        # Request market data type
        ib.reqMarketDataType(1)  # Use real-time data
        
        # Get account info
        st.subheader("Account Summary")
        account_df = get_account_info(ib)
        if account_df is not None:
            st.dataframe(account_df)
        
        # Get positions
        st.subheader("Positions")
        positions_df = get_positions(ib)
        if positions_df is not None:
            st.dataframe(positions_df)
        
        # Test market data
        # Test market data
        st.subheader("Market Data Test")
        try:
            st.info("Requesting AAPL market data...")
            
            # Use delayed data type (3) instead of real-time (1)
            ib.reqMarketDataType(1)
            
            # Create contract and qualify it
            stock = Stock('AAPL', 'SMART', 'USD')
            ib.qualifyContracts(stock)
            
            # Request market data with simplified parameters
            ticker = ib.reqMktData(stock)
            
            # Wait longer and show more details
            progress_bar = st.progress(0)
            st.text("Waiting for market data to arrive...")
            
            # Initialize data display
            data_display = st.empty()
            
            # Wait with more feedback
            for i in range(20):  # Wait longer (10 seconds)
                progress_bar.progress((i+1)/20)
                
                # Show the current state of the ticker
                ticker_data = {
                    'Market Price': ticker.marketPrice(),
                    'Last': ticker.last,
                    'Bid': ticker.bid,
                    'Ask': ticker.ask,
                    'Close': ticker.close,
                    'Time': ticker.time,
                    'Has Market Data': hasattr(ticker, 'marketPrice') and ticker.marketPrice() is not None
                }
                data_display.json(ticker_data)
                
                # If we get a price, we can break early
                if ticker.marketPrice() is not None and ticker.marketPrice() > 0:
                    break
                    
                time.sleep(0.5)
            
            # Final data display
            st.write("AAPL Market Data:")
            
            # More complete ticker data
            final_ticker_data = {
                'Market Price': ticker.marketPrice(),
                'Last': ticker.last,
                'Bid': ticker.bid,
                'Ask': ticker.ask,
                'Close': ticker.close,
                'Open': ticker.open,
                'High': ticker.high,
                'Low': ticker.low,
                'Volume': ticker.volume,
                'Time': ticker.time
            }
            
            st.write(final_ticker_data)
            
            # Check different price attributes in case marketPrice() is not working
            price = ticker.marketPrice()
            if price is None or price <= 0:
                price = ticker.last
                if price is None or price <= 0:
                    price = (ticker.bid + ticker.ask) / 2 if ticker.bid and ticker.ask else None
            
            if price is not None and price > 0:
                st.success(f"Successfully received market price: ${price:.2f}")
            else:
                st.warning("Did not receive a valid market price. Check market data permissions.")
                
            # Show some debugging info about the ticker
            with st.expander("Ticker Debug Info"):
                st.text(f"Ticker has marketPrice attribute: {hasattr(ticker, 'marketPrice')}")
                st.text(f"Ticker modelGreeks: {ticker.modelGreeks}")
                st.text(f"All ticker attributes: {dir(ticker)}")
                
            # Clean up when done by canceling the market data
            ib.cancelMktData(stock)
            
        except Exception as e:
            st.error(f"Error testing market data: {e}")
            import traceback
            st.text(traceback.format_exc())
                
        # Option to disconnect
        if st.button("Disconnect"):
            ib.disconnect()
            st.session_state.pop('ib', None)
            st.success("Disconnected from IB Gateway")
            st.experimental_rerun()
    else:
        st.error("Failed to connect to Interactive Brokers")

# Display connection status
if 'ib' in st.session_state:
    if st.session_state.ib.isConnected():
        st.sidebar.success("Connected to IB Gateway")
    else:
        st.sidebar.error("Disconnected from IB Gateway")
        st.session_state.pop('ib', None)
else:
    st.sidebar.warning("Not connected to IB Gateway")

# Add a note about requirements
st.sidebar.markdown("---")
st.sidebar.info("""
Make sure:
1. IB Gateway is running
2. API connections are enabled
3. Socket port is set to 7497
4. You have market data subscriptions
""")