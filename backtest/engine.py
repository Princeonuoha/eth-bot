"""
Backtest Engine — Vectorized (Fast)
─────────────────────────────────────
Precomputes all indicators upfront using vectorized pandas operations,
then steps through candles doing only lookups. ~50x faster than the
rolling-window version.

Key difference from slow version:
  - Slow: recomputes RSI/EMA/ATR from scratch on every candle
  - Fast: computes full indicator series once, then indexes by position
"""

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd
from loguru import logger


@dataclass
class BacktestTrade:
    entry_time: datetime
    entry_price: float
    quantity: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    reason: str | None = None
    pnl_usdc: float | None = None
    pnl_pct: float | None = None
    peak_price: float = 0.0
    peak_pct: float = 0.0
    partial_exit_price: float | None = None
    partial_pnl_usdc: float | None = None
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0


@dataclass
class BacktestConfig:
    trade_amount_usdc: float = 1000.0
    rsi_oversold: float = 62.0
    rsi_1h_min: float = 45.0
    pullback_min_pct: float = 0.2
    ema_slope_min_pct: float = -0.1
    max_volume_ratio: float = 1.5
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 0.0
    trailing_activation_pct: float = 1.0
    trailing_stop_pct: float = 0.8
    partial_tp_pct: float = 0.8
    partial_tp_ratio: float = 0.5
    atr_multiplier: float = 2.0
    atr_tp_multiplier: float = 1.5
    atr_tp_min_pct: float = 0.8
    atr_tp_max_pct: float = 4.0
    fee_pct: float = 0.1
    slippage_pct: float = 0.05


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[BacktestTrade] = field(default_factory=list)
    start_date: str = ""
    end_date: str = ""
    symbol: str = ""


# ── Vectorized indicator functions ────────────────────────────────────────────

def _rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _ema(closes: pd.Series, period: int) -> pd.Series:
    return closes.ewm(span=period, adjust=False).mean()


def _ema_slope(ema_series: pd.Series, lookback: int = 5) -> pd.Series:
    ema_prev = ema_series.shift(lookback)
    return ((ema_series - ema_prev) / ema_prev.replace(0, np.nan)) * 100


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> pd.Series:
    prev_close = closes.shift(1)
    tr = pd.concat([
        highs - lows,
        (highs - prev_close).abs(),
        (lows - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def _pullback(closes: pd.Series, lookback: int = 20) -> pd.Series:
    rolling_high = closes.rolling(lookback).max()
    return ((rolling_high - closes) / rolling_high.replace(0, np.nan)) * 100


def _volume_ratio(volumes: pd.Series, lookback: int = 20) -> pd.Series:
    avg_vol = volumes.shift(1).rolling(lookback).mean()
    return volumes / avg_vol.replace(0, np.nan)


def _bb_squeeze(closes: pd.Series, bb_period: int = 20, bb_std: float = 2.0,
                kc_period: int = 20, kc_mult: float = 1.5) -> pd.Series:
    bb_mid = closes.rolling(bb_period).mean()
    bb_std_val = closes.rolling(bb_period).std()
    bb_upper = bb_mid + bb_std * bb_std_val
    bb_lower = bb_mid - bb_std * bb_std_val
    tr = closes.diff().abs()
    kc_atr = tr.ewm(com=kc_period - 1, min_periods=kc_period).mean()
    kc_upper = bb_mid + kc_mult * kc_atr
    kc_lower = bb_mid - kc_mult * kc_atr
    return (bb_upper < kc_upper) & (bb_lower > kc_lower)


def _atr_tp(atr_pct: pd.Series, rsi: pd.Series, ema_slope: pd.Series,
            multiplier: float, min_pct: float, max_pct: float) -> pd.Series:
    tp = atr_pct * multiplier
    tp = tp.where(rsi >= 40, tp * 1.10)
    tp = tp.where(rsi >= 30, tp * 1.20).where(rsi >= 30, tp)
    tp = tp.where(rsi < 50, tp * 0.90)
    tp = tp.where(ema_slope <= 0.05, tp * 1.15)
    tp = tp.where(ema_slope >= 0, tp * 0.90)
    return tp.clip(min_pct, max_pct).round(2)


def _precompute(df_15m: pd.DataFrame, df_1h: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compute all indicators once on the full series."""
    logger.info("Precomputing indicators...")

    # 15m indicators
    df = df_15m.copy()
    df["rsi_15m"] = _rsi(df["close"], 14)
    df["pullback"] = _pullback(df["close"], 20)
    df["vol_ratio"] = _volume_ratio(df["volume"], 20)
    df["squeeze"] = _bb_squeeze(df["close"])
    atr_15m = _atr(df["high"], df["low"], df["close"], 14)
    df["atr_pct"] = (atr_15m / df["close"]) * 100

    # 1h indicators
    h = df_1h.copy()
    h["rsi_1h"] = _rsi(h["close"], 14)
    h["ema_200"] = _ema(h["close"], 200)
    h["ema_50"] = _ema(h["close"], 50)
    ema_200 = h["ema_200"]
    h["ema_slope"] = _ema_slope(ema_200, lookback=5)
    atr_1h = _atr(h["high"], h["low"], h["close"], 14)
    h["atr_pct_1h"] = (atr_1h / h["close"]) * 100

    logger.info("Indicators precomputed ✅")
    return df, h


def run(
    df_15m: pd.DataFrame,
    df_1h: pd.DataFrame,
    config: BacktestConfig | None = None,
    symbol: str = "ETHUSDC",
) -> BacktestResult:
    if config is None:
        config = BacktestConfig()

    result = BacktestResult(
        config=config,
        symbol=symbol,
        start_date=str(df_15m["open_time"].iloc[0].date()),
        end_date=str(df_15m["open_time"].iloc[-1].date()),
    )

    # Precompute all indicators upfront
    df_15m_ind, df_1h_ind = _precompute(df_15m, df_1h)

    # Merge 1h indicators onto 15m by forward-filling (as-of join)
    df_1h_ind = df_1h_ind.set_index("open_time").sort_index()
    df_15m_ind = df_15m_ind.set_index("open_time").sort_index()

    # Forward fill 1h values into 15m index
    combined = df_15m_ind.copy()
    for col in ["rsi_1h", "ema_200", "ema_50", "ema_slope"]:
        combined[col] = df_1h_ind[col].reindex(
            combined.index, method="ffill"
        )

    # Drop rows without enough data
    min_idx = 210
    combined = combined.iloc[min_idx:].copy()
    combined = combined.dropna(subset=["rsi_15m", "rsi_1h", "ema_200", "ema_50", "ema_slope"])

    logger.info(
        f"Backtest: {symbol} | {result.start_date} → {result.end_date} | "
        f"{len(combined)} candles after warmup"
    )

    # ── Main loop — now just lookups, no recomputation ────────────────────────
    position: BacktestTrade | None = None
    peak_price: float = 0.0
    active_stop_loss_pct: float = config.stop_loss_pct
    active_take_profit_pct: float = config.take_profit_pct
    partial_done: bool = False

    for candle_time, row in combined.iterrows():
        price = float(row["close"])

        # Read precomputed indicators
        rsi = float(row["rsi_15m"])
        rsi_1h = float(row["rsi_1h"])
        ema_200 = float(row["ema_200"])
        ema_50 = float(row["ema_50"])
        ema_slope = float(row["ema_slope"])
        pullback = float(row["pullback"]) if not pd.isna(row["pullback"]) else 0.0
        vol_ratio = float(row["vol_ratio"]) if not pd.isna(row["vol_ratio"]) else 1.0
        atr_pct = float(row["atr_pct"]) if not pd.isna(row["atr_pct"]) else 0.0
        _ = bool(row["squeeze"]) if not pd.isna(row["squeeze"]) else False

        trend_ok = price > ema_200
        ema_50_above_200 = ema_50 > ema_200

        # ── Update peak ───────────────────────────────────────────────────────
        if position and price > peak_price:
            peak_price = price

        # ── Exit logic ────────────────────────────────────────────────────────
        if position:
            pnl_pct = ((price - position.entry_price) / position.entry_price) * 100
            peak_pct_now = ((peak_price - position.entry_price) / position.entry_price) * 100

            decision = None

            # 1. Hard stop
            if pnl_pct <= -active_stop_loss_pct:
                decision = "stop_loss"

            # 2. Partial TP
            elif config.partial_tp_pct > 0 and not partial_done and pnl_pct >= config.partial_tp_pct:
                decision = "partial_take_profit"

            # 3. Full TP
            elif active_take_profit_pct > 0 and pnl_pct >= active_take_profit_pct:
                decision = "take_profit"

            # 4. Trailing stop
            elif peak_pct_now >= config.trailing_activation_pct:
                trail_level = peak_price * (1 - config.trailing_stop_pct / 100)
                if price <= trail_level:
                    decision = "trailing_stop"

            if decision == "partial_take_profit":
                partial_qty = round(position.quantity * config.partial_tp_ratio, 5)
                remaining_qty = round(position.quantity - partial_qty, 5)
                fill_price = price * (1 - config.slippage_pct / 100)
                fee_cost = fill_price * partial_qty * config.fee_pct / 100
                partial_pnl = (fill_price - position.entry_price) * partial_qty - fee_cost
                position.partial_exit_price = fill_price
                position.partial_pnl_usdc = round(partial_pnl, 4)
                position.quantity = remaining_qty
                partial_done = True

            elif decision in ("stop_loss", "take_profit", "trailing_stop"):
                fill_price = price * (1 - config.slippage_pct / 100)
                fee_cost = fill_price * position.quantity * config.fee_pct / 100
                pnl_usdc = (fill_price - position.entry_price) * position.quantity - fee_cost
                total_pnl = pnl_usdc + (position.partial_pnl_usdc or 0.0)
                peak_pct_final = ((peak_price - position.entry_price) / position.entry_price) * 100

                position.exit_time = candle_time
                position.exit_price = fill_price
                position.reason = decision
                position.pnl_usdc = round(total_pnl, 4)
                position.pnl_pct = round(pnl_pct, 4)
                position.peak_price = peak_price
                position.peak_pct = round(peak_pct_final, 4)

                result.trades.append(position)
                position = None
                peak_price = 0.0
                partial_done = False
                active_stop_loss_pct = config.stop_loss_pct
                active_take_profit_pct = config.take_profit_pct

            continue

        # ── Entry logic ───────────────────────────────────────────────────────
        if (
            not pd.isna(rsi) and not pd.isna(rsi_1h)
            and trend_ok
            and ema_50_above_200
            and ema_slope > config.ema_slope_min_pct
            and rsi_1h >= config.rsi_1h_min
            and rsi < config.rsi_oversold
            and pullback >= config.pullback_min_pct
            and vol_ratio < config.max_volume_ratio
        ):
            # ATR stop
            if config.atr_multiplier > 0 and atr_pct > 0:
                atr_stop = atr_pct * config.atr_multiplier
                active_stop_loss_pct = max(atr_stop, config.stop_loss_pct)
            else:
                active_stop_loss_pct = config.stop_loss_pct

            # ATR TP
            if config.take_profit_pct > 0:
                active_take_profit_pct = config.take_profit_pct
            else:
                tp = atr_pct * config.atr_tp_multiplier
                if rsi < 30:
                    tp *= 1.20
                elif rsi < 40:
                    tp *= 1.10
                elif rsi > 50:
                    tp *= 0.90
                if ema_slope > 0.05:
                    tp *= 1.15
                elif ema_slope < 0:
                    tp *= 0.90
                active_take_profit_pct = round(
                    max(config.atr_tp_min_pct, min(config.atr_tp_max_pct, tp)), 2
                )

            fill_price = price * (1 + config.slippage_pct / 100)
            fee_cost = fill_price * (config.trade_amount_usdc / fill_price) * config.fee_pct / 100
            qty = round((config.trade_amount_usdc - fee_cost) / fill_price, 5)

            position = BacktestTrade(
                entry_time=candle_time,
                entry_price=fill_price,
                quantity=qty,
                peak_price=fill_price,
                stop_loss_pct=active_stop_loss_pct,
                take_profit_pct=active_take_profit_pct,
            )
            peak_price = fill_price
            partial_done = False

    # Close open position at end
    if position:
        last_price = float(combined.iloc[-1]["close"])
        pnl_usdc = (last_price - position.entry_price) * position.quantity
        total_pnl = pnl_usdc + (position.partial_pnl_usdc or 0.0)
        position.exit_time = combined.index[-1]
        position.exit_price = last_price
        position.reason = "end_of_data"
        position.pnl_usdc = round(total_pnl, 4)
        position.pnl_pct = round(((last_price - position.entry_price) / position.entry_price) * 100, 4)
        position.peak_price = peak_price
        position.peak_pct = round(((peak_price - position.entry_price) / position.entry_price) * 100, 4)
        result.trades.append(position)
        logger.info(f"Closed open position at end of data @ ${last_price:.2f}")

    logger.info(f"Backtest complete: {len(result.trades)} trades")
    return result
