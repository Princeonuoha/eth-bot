import pandas as pd
import numpy as np


def compute_rsi(closes: pd.Series, period: int = 14) -> float:
    """Returns the most recent RSI value."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def compute_ema(closes: pd.Series, period: int) -> float:
    """Returns the most recent EMA value."""
    ema = closes.ewm(span=period, adjust=False).mean()
    return float(ema.iloc[-1])


def compute_bollinger_bands(
    closes: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[float, float, float]:
    """Returns (upper_band, middle_band, lower_band) for the last candle."""
    middle = closes.rolling(window=period).mean()
    std = closes.rolling(window=period).std()
    upper = middle + (std * num_std)
    lower = middle - (std * num_std)
    return float(upper.iloc[-1]), float(middle.iloc[-1]), float(lower.iloc[-1])


def compute_pullback_pct(closes: pd.Series, lookback: int = 20) -> float:
    """
    Returns how far price has pulled back from the recent high (as a positive %).
    E.g. 1.5 means price is 1.5% below its recent peak.
    """
    recent_high = closes.tail(lookback).max()
    current = closes.iloc[-1]
    return float(((recent_high - current) / recent_high) * 100)
