"""
Minimal market data test for TWS API
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

# Import nest_asyncio to allow nested event loops
import nest_asyncio
nest_asyncio.apply()

# Now import ib_insync
from ib_insync import *

# Initialize the app
st.set_page_config(
    page_title="IB Market Data Test",
    page_icon="📊",
    layout="wide"
)

st.title("Interactive Brokers Market Data Test")

# Initialize a shared IB instance in session state
if 'ib' not in st.session_state:
    st.session_state.ib = IB()

# Get the IB instance from session state
ib = st.session_state.ib

# Connect to TWS
def connect_to_tws(client_id=None, readonly=True):
    if client_id is None:
        client_id = random.randint(1000, 9999)
    
    try:
        st.info(f"Connecting with client ID: {client_id}...")
        
        # Disconnect first if already connected
        if ib.isConnected():
            ib.disconnect()
        
        # Connect to TWS
        ib.connect('127.0.0.1', 7497, clientId=client_id, readonly=readonly)
        st.success(f"Connected to TWS with client ID {client_id}")
        
        # Display server info
        st.write(f"Server Version: {ib.client.serverVersion()}")
        
        # Untested replacement for Claude's security hole.
        # Get available accounts and select preferred if available
        accounts = ib.client.getAccounts()
        st.write(f"Available accounts: {accounts}")
        
        acct_num = st.text_input("Enter preferred account number")
        if acct_num in accounts:
            ib.client.setAccount(acct_num)
            st.success(f"Selected preferred account: {acct_num}")
        else:
            st.warning("Preferred account not found in available accounts.")
        
        # Claude hard coded the account number.
        #if '#########' in accounts:
        #    ib.client.setAccount('#########')
        #    st.success("Selected preferred account: #########")
        
        return True
    except Exception as e:
        st.error(f"Connection failed: {e}")
        return False

# Initialize session state for market data
if 'market_data' not in st.session_state:
    st.session_state.market_data = {}

# UI for testing connection
st.subheader("Connection Settings")
col1, col2 = st.columns(2)
with col1:
    client_id = st.number_input("Client ID", min_value=1, max_value=9999999, value=random.randint(1000, 9999))
with col2:
    readonly = st.checkbox("Read-only mode", value=True)

if st.button("Connect to TWS"):
    connect_to_tws(client_id=client_id, readonly=readonly)

# Connection status
if ib.isConnected():
    st.success("Connected to TWS")
else:
    st.error("Not connected to TWS")

# Market Data Test Section
st.header("Market Data Tests")

# Ticker symbol input
symbol = st.text_input("Enter Symbol", "AAPL")

# Columns for different test methods
col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("Method 1: reqMktData")
    
    if st.button("Test reqMktData"):
        if not ib.isConnected():
            st.error("Please connect to TWS first")
        else:
            try:
                # Clear previous data
                st.session_state.market_data['method1'] = {'status': 'Running...'}
                
                # Set market data type (1=live, 2=frozen, 3=delayed, 4=delayed frozen)
                for mdt in [3, 1, 2, 4]:
                    st.text(f"Trying market data type: {mdt}")
                    ib.reqMarketDataType(mdt)
                    
                    # Create stock contract
                    contract = Stock(symbol, 'SMART', 'USD')
                    ib.qualifyContracts(contract)
                    
                    # Request market data
                    ticker = ib.reqMktData(contract, '', False, False)
                    
                    # Wait for data
                    st.text("Waiting for data...")
                    for i in range(10):
                        # Display current ticker status
                        st.session_state.market_data['method1'] = {
                            'Market Price': ticker.marketPrice(),
                            'Last': ticker.last,
                            'Bid': ticker.bid,
                            'Ask': ticker.ask,
                            'Time': ticker.time,
                            'Market Data Type': mdt
                        }
                        st.text(f"Polling attempt {i+1}/10")
                        
                        # Use ib.sleep instead of time.sleep
                        # This processes events better
                        ib.sleep(1)
                        
                        # Check if we have data
                        if (ticker.last or ticker.bid or ticker.ask):
                            st.success(f"Got data with market data type {mdt}!")
                            break
                    
                    # Clean up
                    ib.cancelMktData(contract)
                    
                    # If we got data, break out of the loop
                    if (ticker.last or ticker.bid or ticker.ask):
                        break
                
                # Show final status
                st.subheader("Final Result")
                st.json(st.session_state.market_data['method1'])
                
            except Exception as e:
                st.error(f"Error with reqMktData: {e}")

with col2:
    st.subheader("Method 2: Event-Based")
    
    if st.button("Test Event-Based"):
        if not ib.isConnected():
            st.error("Please connect to TWS first")
        else:
            try:
                # Clear previous data
                st.session_state.market_data['method2'] = {'status': 'Running...'}
                
                # Create stock contract
                contract = Stock(symbol, 'SMART', 'USD')
                ib.qualifyContracts(contract)
                
                # Create a result container
                result_container = st.empty()
                
                # Event handler for ticker updates
                def on_ticker_update(ticker):
                    # Update session state
                    st.session_state.market_data['method2'] = {
                        'Market Price': ticker.marketPrice(),
                        'Last': ticker.last,
                        'Bid': ticker.bid,
                        'Ask': ticker.ask,
                        'Time': ticker.time,
                        'Last Update': datetime.now().strftime("%H:%M:%S.%f")
                    }
                
                # Request market data and register the event handler
                ticker = ib.reqMktData(contract)
                ticker.updateEvent += on_ticker_update
                
                # Wait for data with better event processing
                st.text("Waiting for data events...")
                for i in range(10):
                    # Display current status
                    result_container.json(st.session_state.market_data['method2'])
                    
                    # Use ib.sleep to process events properly
                    ib.sleep(1)
                    
                    # Check if we have data
                    if ('Last Update' in st.session_state.market_data['method2']):
                        data = st.session_state.market_data['method2']
                        if (data.get('Last') or data.get('Bid') or data.get('Ask')):
                            st.success("Got data via events!")
                            break
                
                # Clean up
                ib.cancelMktData(contract)
                
                # Show final result
                st.subheader("Final Result")
                st.json(st.session_state.market_data['method2'])
                
            except Exception as e:
                st.error(f"Error with event-based method: {e}")

with col3:
    st.subheader("Method 3: Async Method")
    
    if st.button("Test Async Method"):
        if not ib.isConnected():
            st.error("Please connect to TWS first")
        else:
            try:
                # Clear previous data
                st.session_state.market_data['method3'] = {'status': 'Running...'}
                
                # Create a result container
                result_container = st.empty()
                
                # Define async function
                async def get_market_data_async():
                    # Create stock contract
                    contract = Stock(symbol, 'SMART', 'USD')
                    await ib.qualifyContractsAsync(contract)
                    
                    # Request market data
                    ticker = ib.reqMktData(contract, '', False, False)
                    
                    # Wait for data
                    for i in range(10):
                        # Update status
                        st.session_state.market_data['method3'] = {
                            'Market Price': ticker.marketPrice(),
                            'Last': ticker.last,
                            'Bid': ticker.bid,
                            'Ask': ticker.ask,
                            'Time': ticker.time,
                            'Attempt': i+1
                        }
                        
                        # Display current state
                        result_container.json(st.session_state.market_data['method3'])
                        
                        # Check if we have data
                        if (ticker.last or ticker.bid or ticker.ask):
                            break
                            
                        await asyncio.sleep(1)
                    
                    # Clean up
                    ib.cancelMktData(contract)
                    return ticker
                
                # Run the async function
                st.text("Running async market data request...")
                
                # Execute the async function using ib.run
                ticker = ib.run(get_market_data_async())
                
                # Update final status
                st.session_state.market_data['method3'] = {
                    'Market Price': ticker.marketPrice(),
                    'Last': ticker.last,
                    'Bid': ticker.bid,
                    'Ask': ticker.ask,
                    'Time': ticker.time,
                    'Final': True
                }
                
                # Show result
                st.subheader("Result")
                if (ticker.last or ticker.bid or ticker.ask):
                    st.success("Got data with async method!")
                else:
                    st.warning("Async method did not get price data")
                    
                st.json(st.session_state.market_data['method3'])
                
            except Exception as e:
                st.error(f"Error with async method: {e}")
                import traceback
                st.text(traceback.format_exc())

# Show TWS diagnostic info
st.header("TWS Diagnostic Information")

if st.button("Check TWS Status"):
    if not ib.isConnected():
        st.error("Please connect to TWS first")
    else:
        try:
            # Get TWS information
            info = {
                'Server Version': ib.client.serverVersion(),
                'Connected': ib.isConnected(),
                'Client ID': ib.client.clientId,
                'Accounts': ib.client.getAccounts(),
            }
            
            # Try to get account summary
            try:
                account_summary = ib.accountSummary()
                info['Has Account Summary'] = len(account_summary) > 0
                info['Account Tags'] = [summary.tag for summary in account_summary[:5]]
            except Exception as e:
                info['Account Summary Error'] = str(e)
            
            # Try to get market data type
            try:
                ib.reqMarketDataType(3)  # Set to delayed
                info['Market Data Type'] = "Requested Delayed (3)"
            except Exception as e:
                info['Market Data Type Error'] = str(e)
            
            # Display info
            st.subheader("TWS Status")
            st.json(info)
            
            # TWS settings to check
            st.subheader("TWS Settings to Check")
            st.markdown("""
            1. In TWS, go to **Edit > Global Configuration > API > Settings**
            2. Make sure "Enable ActiveX and Socket Clients" is **checked**
            3. Check that your Socket port is set to **7497**
            4. Set all Precautions to "Bypass" temporarily
            5. Check if you have market data subscriptions for the symbols you're testing
            6. Try restarting TWS completely
            7. Check the "API" tab in TWS for any access messages
            """)
            
        except Exception as e:
            st.error(f"Error checking TWS status: {e}")

# Disconnect button
if ib.isConnected() and st.button("Disconnect"):
    ib.disconnect()
    st.success("Disconnected from TWS")
    
# Proper cleanup on exit
import atexit

def cleanup():
    """Clean up resources before exit"""
    try:
        if ib.isConnected():
            print("Disconnecting from TWS...")
            ib.disconnect()
        print("Cleanup complete.")
    except Exception as e:
        print(f"Error during cleanup: {e}")

# Register cleanup function to be called on exit
atexit.register(cleanup)