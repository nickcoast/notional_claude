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
        st.subheader("Market Data Test")
        try:
            st.info("Requesting AAPL market data using events...")
            
            # Create a place to store the ticker data
            if 'ticker_data' not in st.session_state:
                st.session_state.ticker_data = None
            
            # Function to handle ticker updates
            def on_ticker_update(ticker):
                # Store the updated ticker data in session state
                st.session_state.ticker_data = {
                    'Market Price': ticker.marketPrice(),
                    'Last': ticker.last,
                    'Bid': ticker.bid,
                    'Ask': ticker.ask,
                    'Close': ticker.close,
                    'Volume': ticker.volume,
                    'Time': ticker.time,
                    'Has Data': ticker.last is not None or ticker.bid is not None or ticker.ask is not None
                }
            
            # Create contract and qualify it
            stock = Stock('AAPL', 'SMART', 'USD')
            ib.qualifyContracts(stock)
            
            # Try alternative methods
            st.text("Method 1: Using reqMktData with events")
            
            # Request market data and register event handler
            ticker = ib.reqMktData(stock, '', False, False)
            ticker.updateEvent += on_ticker_update
            
            # Wait with progress bar
            progress_bar = st.progress(0)
            data_display = st.empty()
            
            for i in range(20):
                progress_bar.progress((i+1)/20)
                
                # Show the current state of the data
                if st.session_state.ticker_data:
                    data_display.json(st.session_state.ticker_data)
                    
                    # If we have actual data, break early
                    if st.session_state.ticker_data.get('Has Data'):
                        break
                else:
                    data_display.text("Waiting for ticker data...")
                    
                # Allow events to process
                ib.sleep(0.5)
            
            # If first method failed, try method 2
            if not st.session_state.ticker_data or not st.session_state.ticker_data.get('Has Data'):
                st.text("Method 1 failed. Trying Method 2: Using reqTickers")
                ib.cancelMktData(stock)  # Cancel the previous request
                
                # Try using reqTickers instead
                tickers = ib.reqTickers(stock)
                if tickers:
                    st.session_state.ticker_data = {
                        'Market Price': tickers[0].marketPrice(),
                        'Last': tickers[0].last,
                        'Bid': tickers[0].bid,
                        'Ask': tickers[0].ask,
                        'Method': 'reqTickers'
                    }
                    data_display.json(st.session_state.ticker_data)
            
            # If second method also failed, try method 3
            if not st.session_state.ticker_data or not st.session_state.ticker_data.get('Has Data'):
                st.text("Method 2 failed. Trying Method 3: Manual market snapshot")
                
                # Try one more approach - get a complete market snapshot
                contract = Stock('AAPL', 'SMART', 'USD')
                contract.exchange = 'SMART'
                ib.qualifyContracts(contract)
                
                # Request a complete market snapshot
                ticker = ib.reqMktData(contract, 'mdoff,233', False, False)
                ib.sleep(2)  # Wait for the snapshot
                
                # Check the data
                st.session_state.ticker_data = {
                    'Contract': str(ticker.contract),
                    'Last': ticker.last,
                    'Bid': ticker.bid, 
                    'Ask': ticker.ask,
                    'Volume': ticker.volume,
                    'Method': 'Market Snapshot'
                }
                data_display.json(st.session_state.ticker_data)
            
            # Final check
            has_data = False
            if st.session_state.ticker_data:
                price = None
                data = st.session_state.ticker_data
                
                if data.get('Last'):
                    price = data['Last']
                    has_data = True
                elif data.get('Bid') and data.get('Ask'):
                    price = (data['Bid'] + data['Ask']) / 2
                    has_data = True
                elif data.get('Market Price'):
                    price = data['Market Price']
                    has_data = True
                    
                if has_data and price:
                    st.success(f"Successfully received price data: ${price:.2f}")
                else:
                    st.warning("Could not get valid price data through normal means")
            
            # Show some API diagnostic info
            with st.expander("API Diagnostic Info"):
                # Check API connectivity
                managed_accounts = ib.client.getAccounts()
                st.text(f"Connected accounts: {managed_accounts}")
                
                # Check market data permissions
                st.text("Checking market data permissions...")
                try:
                    permissions = ib.reqMarketDataType(3)  # Request delayed data
                    st.text(f"Market data type set to: 3 (Delayed)")
                    
                    # Try to check permissions directly
                    snapshot_permissions = ticker.snapshotPermissions if hasattr(ticker, 'snapshotPermissions') else "Unknown"
                    st.text(f"Snapshot permissions: {snapshot_permissions}")
                except Exception as e:
                    st.text(f"Error checking permissions: {e}")
                    
                # Show TWS settings that might be relevant
                st.text("\nTWS Settings to Check:")
                st.text("1. Edit > Global Configuration > API > Settings")
                st.text("2. Market Data > Market Data Connections")
                st.text("3. Make sure 'Enable API' is checked")
                st.text("4. Try with 'Precautions' set to 'Bypass'")
                    
            # Clean up market data requests
            try:
                ib.cancelMktData(stock)
            except:
                pass
                
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