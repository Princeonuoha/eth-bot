import numpy as np
import pandas as pd


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


def compute_ema_slope(closes: pd.Series, period: int = 200, lookback: int = 5) -> float:
    """
    Returns the EMA slope as a percentage change over `lookback` candles.
    Positive = rising EMA (uptrend), Negative = falling EMA (downtrend).
    Example: 0.05 means the 200 EMA has risen 0.05% over the last 5 candles.
    """
    if len(closes) < period + lookback:
        return 0.0
    ema_series = closes.ewm(span=period, adjust=False).mean()
    ema_now = float(ema_series.iloc[-1])
    ema_prev = float(ema_series.iloc[-(lookback + 1)])
    if ema_prev == 0:
        return 0.0
    return ((ema_now - ema_prev) / ema_prev) * 100


def compute_atr(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> float:
    """
    Returns the most recent Average True Range (ATR) value.
    ATR measures market volatility — higher = more volatile.
    Used to set dynamic stop loss distances that adapt to market conditions.
    """
    prev_close = closes.shift(1)
    tr = pd.concat(
        [
            highs - lows,
            (highs - prev_close).abs(),
            (lows - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(com=period - 1, min_periods=period).mean()
    return float(atr.iloc[-1])


def compute_atr_stop_pct(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    period: int = 14,
    multiplier: float = 2.0,
) -> float:
    """
    Returns an ATR-based stop loss distance as a percentage of current price.
    Example: returns 2.1 means place stop 2.1% below entry.
    Always at least 1.0% to avoid stops that are too tight.
    """
    atr = compute_atr(highs, lows, closes, period)
    current_price = float(closes.iloc[-1])
    if current_price == 0:
        return 1.5
    atr_pct = (atr / current_price) * 100 * multiplier
    return max(atr_pct, 1.0)  # floor at 1% minimum


def compute_volume_ratio(volumes: pd.Series, lookback: int = 20) -> float:
    """
    Returns current volume as a ratio of the recent average.
    > 1.5 on a down candle = panic selling / capitulation — avoid buying.
    < 1.0 on a pullback = low-volume dip = healthy pullback — good to buy.
    """
    if len(volumes) < lookback + 1:
        return 1.0
    avg_volume = float(volumes.iloc[-(lookback + 1) : -1].mean())
    current_volume = float(volumes.iloc[-1])
    if avg_volume == 0:
        return 1.0
    return current_volume / avg_volume


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
