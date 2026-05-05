import asyncio
import locale
import logging
import time
from functools import wraps

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def setup_asyncio_event_loop():
    """Ensure there is an event loop available for the current thread."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("Current event loop is closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop


def time_operation(operation_name):
    """Decorator to time operations and log start/end/error."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            logger.info("Starting %s...", operation_name)
            try:
                result = func(*args, **kwargs)
                logger.info("Completed %s in %.2fs", operation_name, time.time() - start_time)
                return result
            except Exception as e:
                logger.error("Error in %s after %.2fs: %s", operation_name, time.time() - start_time, e)
                raise
        return wrapper
    return decorator


def safe_float_conversion(value_str):
    """Safely convert a string to float, handling various formats."""
    if value_str is None:
        return 0.0

    if isinstance(value_str, str):
        if not value_str.strip():
            return 0.0
        clean_str = value_str.replace(locale.localeconv()['currency_symbol'], '')
        clean_str = clean_str.replace(',', '')
        try:
            value = float(clean_str)
            return value if np.isfinite(value) else 0.0
        except ValueError:
            logger.warning("Could not convert %r to float", value_str)
            return 0.0

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


def chunked(items, size):
    """Yield fixed-size chunks from a list-like collection."""
    for idx in range(0, len(items), size):
        yield items[idx:idx + size]


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
