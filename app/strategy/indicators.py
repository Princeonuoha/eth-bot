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
    tr = pd.concat([
        highs - lows,
        (highs - prev_close).abs(),
        (lows - prev_close).abs(),
    ], axis=1).max(axis=1)
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


def compute_atr_take_profit_pct(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    rsi: float,
    ema_slope: float,
    period: int = 14,
    multiplier: float = 1.5,
    min_tp_pct: float = 0.8,
    max_tp_pct: float = 4.0,
) -> float:
    """
    Computes a dynamic take profit target based on current market conditions.
    Returns a percentage (e.g. 1.8 means exit when PnL hits +1.8%).

    Three factors influence the target:

    1. ATR (volatility) — base target width.
       High ATR = wider TP, low ATR = tighter TP.
       Formula: atr_pct * multiplier

    2. RSI — momentum adjustment.
       RSI < 30  → price deeply oversold, strong bounce likely  → +20% wider TP
       RSI 30-40 → moderately oversold, decent bounce expected  → +10% wider TP
       RSI 40-50 → mild dip, modest bounce expected             → no adjustment
       RSI > 50  → not oversold at all (testnet loose settings) → -10% tighter TP

    3. EMA slope — trend strength adjustment.
       Strong rising slope (>0.05%) → trend is accelerating → +15% wider TP
       Flat slope (0 to 0.05%)      → trend just started    → no adjustment
       Negative slope               → weak trend            → -10% tighter TP

    Example:
      ATR = $35, price = $2200 → atr_pct = 1.59%
      Base TP = 1.59 * 1.5 = 2.39%
      RSI = 35 (oversold) → * 1.10 = 2.63%
      EMA slope = 0.06% (rising) → * 1.15 = 3.02%
      Clamped to [0.8%, 4.0%] → TP = 3.02% ✅

    Always clamped between min_tp_pct and max_tp_pct.
    """
    atr = compute_atr(highs, lows, closes, period)
    current_price = float(closes.iloc[-1])
    if current_price == 0:
        return min_tp_pct

    # Base: ATR as % of price * multiplier
    atr_pct = (atr / current_price) * 100
    tp = atr_pct * multiplier

    # RSI adjustment — deeper oversold = more room to bounce
    if rsi < 30:
        tp *= 1.20
    elif rsi < 40:
        tp *= 1.10
    elif rsi > 50:
        tp *= 0.90  # not oversold — be conservative

    # EMA slope adjustment — stronger trend = more room to run
    if ema_slope > 0.05:
        tp *= 1.15   # EMA visibly rising — trend has momentum
    elif ema_slope < 0:
        tp *= 0.90   # EMA flat/falling — take profit quicker

    # Clamp to safe range
    tp = max(min_tp_pct, min(max_tp_pct, tp))

    return round(tp, 2)


def compute_volume_ratio(volumes: pd.Series, lookback: int = 20) -> float:
    """
    Returns current volume as a ratio of the recent average.
    > 1.5 on a down candle = panic selling / capitulation — avoid buying.
    < 1.0 on a pullback = low-volume dip = healthy pullback — good to buy.
    """
    if len(volumes) < lookback + 1:
        return 1.0
    avg_volume = float(volumes.iloc[-(lookback + 1):-1].mean())
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


def compute_ema_50(closes: pd.Series) -> float:
    """Returns the most recent 50 EMA value."""
    return compute_ema(closes, 50)


def compute_bb_squeeze(
    closes: pd.Series,
    bb_period: int = 20,
    bb_std: float = 2.0,
    kc_period: int = 20,
    kc_mult: float = 1.5,
) -> bool:
    """
    Returns True if Bollinger Bands are inside Keltner Channels (squeeze active).

    A BB squeeze means volatility has compressed — price is coiling.
    This often precedes a sharp move in either direction.
    Used as a confirmation: only enter if squeeze is active (compressed volatility
    on a pullback = higher probability explosive move upward when trend is bullish).

    How it works:
      Bollinger Bands use standard deviation — they widen with volatility.
      Keltner Channels use ATR — they're smoother and less reactive.
      When BB is inside KC, volatility is unusually low = coiling = squeeze.
    """
    if len(closes) < max(bb_period, kc_period) + 1:
        return False

    # Bollinger Bands
    bb_mid = closes.rolling(bb_period).mean()
    bb_std_val = closes.rolling(bb_period).std()
    bb_upper = bb_mid + bb_std * bb_std_val
    bb_lower = bb_mid - bb_std * bb_std_val

    # Keltner Channels (using ATR approximation via high-low range)
    tr = closes.diff().abs()  # simplified TR using close-to-close
    kc_atr = tr.ewm(com=kc_period - 1, min_periods=kc_period).mean()
    kc_upper = bb_mid + kc_mult * kc_atr
    kc_lower = bb_mid - kc_mult * kc_atr

    # Squeeze = BB inside KC
    squeeze = (
        float(bb_upper.iloc[-1]) < float(kc_upper.iloc[-1]) and
        float(bb_lower.iloc[-1]) > float(kc_lower.iloc[-1])
    )
    return squeeze
