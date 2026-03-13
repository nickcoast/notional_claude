import streamlit as st

# MUST be first Streamlit command
st.set_page_config(
    page_title="IB Portfolio Viewer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

import asyncio
import logging
import random
import sys
import time
import traceback
from datetime import datetime

import pandas as pd

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

# eventkit (used by ib_insync) expects an event loop during import.
# utils has no ib_insync dependency, so it's safe to import first.
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
from utils import configure_locale, format_currency, get_account_value, setup_asyncio_event_loop
setup_asyncio_event_loop()

# ib_insync-dependent imports come after the event loop is ready.
from ib_insync import IB, Option, Stock
from market_data import run_async
import portfolio as portfolio_module

active_locale = configure_locale()
if active_locale:
    logger.info(f"Using locale: {active_locale}")
else:
    logger.warning("No supported locale found; currency formatting will use fallback.")


@st.cache_resource
def get_ib():
    ib = IB()
    # Avoid indefinite hangs on synchronous request wrappers.
    ib.RequestTimeout = 20
    return ib


ib = get_ib()


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
            ib.reqMarketDataType(portfolio_module.PREFERRED_MARKET_DATA_TYPE)
            logger.info("Market data type set to %s", portfolio_module.PREFERRED_MARKET_DATA_TYPE)
        except Exception as market_data_type_error:
            logger.warning("Could not set preferred market data type: %s", market_data_type_error)

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


def get_portfolio_data():
    if not ib.isConnected():
        return None, None, None, None
    try:
        return portfolio_module.get_portfolio_data_sync(
            ib,
            option_delta_cache=st.session_state.setdefault('option_delta_cache', {}),
            underlying_price_cache=st.session_state.setdefault('underlying_price_cache', {}),
            force_retry_now=st.session_state.pop('force_retry_quotes_now', False),
            auto_retry_enabled=st.session_state.get('auto_retry_quotes_enabled', False),
        )
    except Exception as e:
        st.error(f"Error getting portfolio data: {e}")
        return None, None, None, None


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


# ── Options browser (async helpers) ──────────────────────────────────────────

async def async_get_option_chain(ticker):
    stock = Stock(ticker, 'SMART', 'USD')
    await ib.qualifyContractsAsync(stock)

    ticker_data = ib.reqMktData(stock)
    await asyncio.sleep(0.2)
    stock_price = ticker_data.marketPrice()

    chains = await ib.reqSecDefOptParamsAsync(stock.symbol, '', stock.secType, stock.conId)

    expirations = []
    for chain in chains:
        if chain.exchange == 'SMART':
            expirations = sorted(chain.expirations)
            break

    return stock_price, expirations


async def async_get_options_for_expiration(ticker, expiration):
    stock = Stock(ticker, 'SMART', 'USD')
    await ib.qualifyContractsAsync(stock)

    ticker_data = ib.reqMktData(stock)
    await asyncio.sleep(0.2)
    stock_price = ticker_data.marketPrice()

    chains = await ib.reqSecDefOptParamsAsync(stock.symbol, '', stock.secType, stock.conId)

    chain = next((c for c in chains if c.exchange == 'SMART'), None)
    if not chain:
        return None, None, None

    strikes = sorted(chain.strikes)
    calls = []
    puts = []

    for strike in strikes:
        call_contract = Option(ticker, expiration, strike, 'C', 'SMART')
        put_contract = Option(ticker, expiration, strike, 'P', 'SMART')

        await ib.qualifyContractsAsync(call_contract, put_contract)

        call_ticker = ib.reqMktData(call_contract)
        await asyncio.sleep(0.1)
        put_ticker = ib.reqMktData(put_contract)
        await asyncio.sleep(0.1)

        call_price = call_ticker.marketPrice()
        call_delta = None
        call_gamma = None
        if hasattr(call_ticker, 'modelGreeks') and call_ticker.modelGreeks:
            call_delta = call_ticker.modelGreeks.delta
            call_gamma = call_ticker.modelGreeks.gamma
        else:
            call_delta = 0.7 if stock_price > strike else 0.3
            call_gamma = 0.01

        put_price = put_ticker.marketPrice()
        put_delta = None
        put_gamma = None
        if hasattr(put_ticker, 'modelGreeks') and put_ticker.modelGreeks:
            put_delta = put_ticker.modelGreeks.delta
            put_gamma = put_ticker.modelGreeks.gamma
        else:
            put_delta = -0.7 if stock_price < strike else -0.3
            put_gamma = 0.01

        call_pct = (call_price / stock_price) * 100 if stock_price > 0 else 0
        put_pct = (put_price / stock_price) * 100 if stock_price > 0 else 0
        call_diff = call_price - (stock_price - strike) if stock_price > strike else call_price
        put_diff = put_price - (strike - stock_price) if stock_price < strike else put_price

        calls.append({
            'Strike': strike,
            'Bid': call_ticker.bid, 'Ask': call_ticker.ask, 'Last': call_ticker.last,
            'Price': call_price, 'Delta': call_delta, 'Gamma': call_gamma,
            'Pct of Stock': f"{call_pct:.2f}%", 'Diff from Stock': call_diff,
        })
        puts.append({
            'Strike': strike,
            'Bid': put_ticker.bid, 'Ask': put_ticker.ask, 'Last': put_ticker.last,
            'Price': put_price, 'Delta': put_delta, 'Gamma': put_gamma,
            'Pct of Stock': f"{put_pct:.2f}%", 'Diff from Stock': put_diff,
        })

    return stock_price, calls, puts


def get_option_chain(ticker):
    if not ib.isConnected():
        return None, None
    try:
        return run_async(async_get_option_chain(ticker))
    except Exception as e:
        st.error(f"Error getting option chain: {e}")
        return None, None


def get_options_for_expiration(ticker, expiration):
    if not ib.isConnected():
        return None, None, None
    try:
        return run_async(async_get_options_for_expiration(ticker, expiration))
    except Exception as e:
        st.error(f"Error getting options data: {e}")
        return None, None, None


# ── UI Layout ─────────────────────────────────────────────────────────────────

st.title("Interactive Brokers Portfolio Viewer")

st.sidebar.title("IB Connection Status")
connection_status = st.sidebar.empty()
diagnostic_status = st.sidebar.empty()
diagnostic_detail = st.sidebar.empty()

if not ib.isConnected():
    if st.sidebar.button("Connect to TWS"):
        connect_to_ib()

if ib.isConnected():
    connection_status.success("Connected to TWS")
else:
    connection_status.error("Not connected to TWS")

st.header("Portfolio Metrics")
portfolio_metrics = st.empty()

main_content = st.container()

st.header("Options Browser")
search_col, expiry_col = st.columns([1, 3])
with search_col:
    ticker_input = st.text_input("Enter Ticker Symbol", "")
    search_button = st.button("Search Options")

expiration_container = expiry_col.empty()
options_display = st.empty()

auto_refresh = st.sidebar.checkbox("Auto Refresh", value=False)
refresh_interval = None
if auto_refresh:
    refresh_interval = st.sidebar.slider("Refresh Interval (seconds)", 5, 60, 10)

auto_retry_quotes = st.sidebar.checkbox("Auto Retry Weak Quotes", value=False)
st.session_state['auto_retry_quotes_enabled'] = auto_retry_quotes
if st.sidebar.button("Retry Weak Quotes Now"):
    st.session_state['force_retry_quotes_now'] = True
    st.rerun()
st.sidebar.caption(f"Market data type: {portfolio_module.PREFERRED_MARKET_DATA_TYPE}")

if st.sidebar.button("Clear Quote Cache"):
    portfolio_module.clear_quote_cache()
    st.session_state.pop('underlying_price_cache', None)
    st.session_state.pop('option_delta_cache', None)
    st.sidebar.success("Cleared quote cache.")

retry_state_preview = portfolio_module._runtime_cache.get('quote_retry_state', {})
if retry_state_preview:
    retry_symbols = sorted(retry_state_preview.keys())
    preview = ", ".join(retry_symbols[:6])
    if len(retry_symbols) > 6:
        preview = f"{preview}, ..."
    st.sidebar.caption(f"Pending quote retries ({len(retry_symbols)}): {preview}")
    if auto_retry_quotes and not auto_refresh:
        st.sidebar.caption("Retries run on each manual refresh/click. Enable Auto Refresh for continuous retry cycles.")


# ── Main execution ────────────────────────────────────────────────────────────

def main():
    """Main function that runs within Streamlit's execution model."""
    portfolio_module.register_ib_error_handler(ib)

    logger.info("Starting application")

    if not ib.isConnected():
        logger.info("Attempting connection to TWS")
        try:
            connection_success = connect_to_ib()
        except Exception as conn_error:
            logger.error("Unhandled exception in connect_to_ib: %s", conn_error)
            logger.error(traceback.format_exc())
            connection_success = False
    else:
        connection_success = True

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
        try:
            account_df, underlying_df, _, health = get_portfolio_data()
            if health is not None:
                st.session_state['last_data_health'] = health

            if account_df is not None and underlying_df is not None:
                with portfolio_metrics.container():
                    try:
                        metrics_cols = st.columns(6)
                        nlv = get_account_value(account_df, 'NetLiquidation', numeric=True, default=0.0)
                        gross_pos_val = get_account_value(account_df, 'GrossPositionValue', numeric=True, default=0.0)
                        ngav = get_account_value(account_df, 'NGAV (Notional Gross Asset Value)', numeric=True, default=0.0)
                        nlr = get_account_value(account_df, 'NLR (Notional Leverage Ratio)', numeric=True, default=0.0)
                        std_leverage = get_account_value(account_df, 'Standard Leverage Ratio', numeric=True, default=0.0)
                        buying_power = get_account_value(account_df, 'BuyingPower', numeric=True, default=0.0)

                        metrics_cols[0].metric("Net Liquidation Value", format_currency(nlv))
                        metrics_cols[1].metric("Gross Position Value", format_currency(gross_pos_val))
                        metrics_cols[2].metric("NGAV", format_currency(ngav))
                        metrics_cols[3].metric("Standard Leverage", f"{std_leverage:.2f}x")
                        metrics_cols[4].metric("Notional Leverage Ratio", f"{nlr:.2f}x")
                        metrics_cols[5].metric("Buying Power", format_currency(buying_power))
                    except Exception as e:
                        logger.error("Error updating metrics: %s", e)

                with main_content:
                    try:
                        st.subheader("Positions by Underlying")
                        display_df = underlying_df.copy()
                        numeric_cols = [
                            'Stock Count', 'Stock Value', 'Option Notional (Shares)',
                            'Option Notional Value', 'Option Actual Value',
                            'Underlying Market Price', 'Underlying Cost Basis',
                            'Underlying Price', 'Notional Position Value (NPV)',
                        ]
                        for col in numeric_cols:
                            if col in display_df.columns:
                                display_df[col] = pd.to_numeric(display_df[col], errors='coerce')

                        column_config = {}
                        currency_cols = [
                            'Stock Value', 'Option Notional Value', 'Option Actual Value',
                            'Underlying Market Price', 'Underlying Cost Basis',
                            'Underlying Price', 'Notional Position Value (NPV)',
                        ]
                        for col in currency_cols:
                            if col in display_df.columns:
                                column_config[col] = st.column_config.NumberColumn(col, format="$%.2f")

                        if 'Option Notional (Shares)' in display_df.columns:
                            column_config['Option Notional (Shares)'] = st.column_config.NumberColumn(
                                'Option Notional (Shares)', format="%.2f"
                            )
                        if 'Stock Count' in display_df.columns:
                            column_config['Stock Count'] = st.column_config.NumberColumn(
                                'Stock Count', format="%.4f"
                            )

                        st.dataframe(display_df, use_container_width=True, column_config=column_config)
                        st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    except Exception as table_error:
                        logger.error("Error updating positions table: %s", table_error)
        except Exception as update_error:
            logger.error("Error updating portfolio data: %s", update_error)

        if ticker_input and search_button:
            try:
                stock_price, expirations = get_option_chain(ticker_input)
                if stock_price is not None and expirations:
                    with expiration_container.container():
                        exp_dates = [datetime.strptime(exp, '%Y%m%d').strftime('%Y-%m-%d') for exp in expirations]
                        selected_exp_index = st.select_slider(
                            "Select Expiration Date",
                            options=range(len(exp_dates)),
                            format_func=lambda i: exp_dates[i]
                        )
                        selected_exp = expirations[selected_exp_index]

                    stock_price, calls, puts = get_options_for_expiration(ticker_input, selected_exp)
                    if stock_price is not None and calls and puts:
                        with options_display.container():
                            st.subheader(f"{ticker_input} Options - Stock Price: ${stock_price:.2f}")
                            calls_df = pd.DataFrame(calls)
                            puts_df = pd.DataFrame(puts)
                            cols = st.columns([4, 2, 4])
                            with cols[0]:
                                st.subheader("CALLS")
                                st.dataframe(calls_df[['Bid', 'Ask', 'Last', 'Price', 'Delta', 'Gamma', 'Pct of Stock', 'Diff from Stock']], use_container_width=True)
                            with cols[1]:
                                st.subheader("Strike")
                                st.dataframe(calls_df[['Strike']], use_container_width=True)
                            with cols[2]:
                                st.subheader("PUTS")
                                st.dataframe(puts_df[['Bid', 'Ask', 'Last', 'Price', 'Delta', 'Gamma', 'Pct of Stock', 'Diff from Stock']], use_container_width=True)
                            st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception as options_error:
                logger.error("Error updating options data: %s", options_error)

        render_connection_diagnostics()

        if auto_refresh and refresh_interval:
            logger.debug("Auto refresh rerun in %ss", refresh_interval)
            time.sleep(refresh_interval)
            st.rerun()
    else:
        st.error("Failed to connect to Interactive Brokers. Please make sure TWS or IB Gateway is running.")


if __name__ == "__main__":
    st.sidebar.info("""
    This app connects to Interactive Brokers TWS API to display portfolio data.
    Make sure TWS or IB Gateway is running before connecting.
    """)

    if st.sidebar.button("Refresh Data"):
        st.rerun()

    main()
