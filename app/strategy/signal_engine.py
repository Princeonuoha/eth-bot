"""
Signal Engine
─────────────
Decides WHEN to buy and WHEN to sell based on indicator readings.
All logic is pure functions — easy to test, no side effects.

Entry strategy (Phase 2):
  - Trend filter      : price is above 200 EMA on the 1h timeframe
  - Golden cross      : 50 EMA above 200 EMA — medium-term trend aligned
  - EMA slope         : 200 EMA must be rising (slope > threshold) — prevents
                        buying into a downtrending market that briefly pokes above EMA
  - 1h RSI gate       : 1h RSI > rsi_1h_min — higher timeframe not bearish
  - Pullback          : RSI(14) < rsi_oversold AND price is X% below recent high
  - Volume filter     : current candle volume must not be a panic spike
                        (volume_ratio < max_volume_ratio) — avoids buying capitulation
  - BB squeeze        : optional bonus confirmation — volatility compressed = coiling
  - One position at a time

Exit strategy (hybrid — partial TP + trailing):
  - Hard stop loss       : PnL <= -stop_loss_pct (protects against big drops)
  - Partial take profit  : at partial_tp_pct%, sell partial_tp_ratio of position
                           (e.g. 50% at +0.8%) — banks quick wins on ranging days
  - Trailing stop        : activates at trailing_activation_pct%, trails the remainder
                           — rides big moves after partial has been taken
  - Full take profit     : optional hard exit for the remainder if set > 0
"""

from loguru import logger


def is_trend_bullish(current_price: float, ema_200: float) -> bool:
    """Macro trend filter: only buy when price is above the 200 EMA."""
    return current_price > ema_200


def is_ema_slope_bullish(ema_slope: float, min_slope_pct: float = 0.0) -> bool:
    """
    Returns True if the 200 EMA is rising (slope > min_slope_pct).

    Why this matters:
      A rising EMA = the trend is strengthening upward.
      A flat/falling EMA = price is just touching EMA from below during a downtrend.
      The March 22 losing trade entered when ETH briefly crossed EMA during a
      downtrend — a positive slope check would have blocked it.

    min_slope_pct=0.0 means any positive slope passes.
    Use 0.02 or 0.05 for a stricter filter.
    """
    return ema_slope > min_slope_pct


def should_buy(
    *,
    trend_bullish: bool,
    ema_slope: float,
    rsi: float,
    rsi_1h: float,
    pullback_pct: float,
    volume_ratio: float,
    in_position: bool,
    ema_50_above_200: bool,
    bb_squeeze: bool,
    rsi_oversold: float,
    pullback_min_pct: float,
    ema_slope_min_pct: float = 0.0,
    max_volume_ratio: float = 1.5,
    rsi_1h_min: float = 45.0,
) -> bool:
    """
    Returns True if ALL entry conditions are met.

    Filters (in order):
      1. Not already in position
      2. Price above 200 EMA (macro trend)
      3. 50 EMA above 200 EMA (golden cross zone — medium trend aligned)
      4. EMA slope rising (not just touching from below during downtrend)
      5. 1h RSI > rsi_1h_min (higher timeframe not oversold/bearish)
      6. 15m RSI < rsi_oversold (short timeframe dip = entry opportunity)
      7. Pullback > pullback_min_pct (meaningful dip, not just noise)
      8. Volume ratio < max_volume_ratio (no panic candles)
      9. BB squeeze is logged as bonus info but does NOT block entry —
         it upgrades signal quality when present.
    """
    if in_position:
        logger.debug("Signal: skip — already in position")
        return False

    if not trend_bullish:
        logger.debug("Signal: skip — price below 200 EMA")
        return False

    if not ema_50_above_200:
        logger.debug("Signal: skip — 50 EMA below 200 EMA (no golden cross)")
        return False

    if not is_ema_slope_bullish(ema_slope, ema_slope_min_pct):
        logger.debug(
            f"Signal: skip — EMA slope {ema_slope:.4f}% below min {ema_slope_min_pct}% "
            f"(downtrend protection)"
        )
        return False

    if rsi_1h < rsi_1h_min:
        logger.debug(
            f"Signal: skip — 1h RSI {rsi_1h:.1f} below min {rsi_1h_min} "
            f"(higher timeframe bearish)"
        )
        return False

    if rsi >= rsi_oversold:
        logger.debug(
            f"Signal: skip — 15m RSI {rsi:.1f} not oversold (threshold {rsi_oversold})"
        )
        return False

    if pullback_pct < pullback_min_pct:
        logger.debug(
            f"Signal: skip — pullback {pullback_pct:.2f}% too small (min {pullback_min_pct}%)"
        )
        return False

    if volume_ratio >= max_volume_ratio:
        logger.debug(
            f"Signal: skip — volume spike {volume_ratio:.2f}x avg "
            f"(max {max_volume_ratio}x) — possible panic candle, avoid buying"
        )
        return False

    squeeze_note = " | BB squeeze 🔥" if bb_squeeze else ""
    logger.info(
        f"Signal: BUY ✅ | 15m RSI={rsi:.1f} | 1h RSI={rsi_1h:.1f} | "
        f"pullback={pullback_pct:.2f}% | ema_slope={ema_slope:.4f}% | "
        f"50>200=✅ | vol={volume_ratio:.2f}x{squeeze_note}"
    )
    return True


def should_sell(
    *,
    entry_price: float,
    current_price: float,
    peak_price: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    trailing_activation_pct: float,
    trailing_stop_pct: float,
    partial_tp_pct: float = 0.0,
    partial_done: bool = False,
) -> str | None:
    """
    Exit logic — four layers, checked in priority order:

    1. Hard stop loss      — fires if PnL <= -stop_loss_pct. Always active.

    2. Partial take profit — fires ONCE when PnL hits partial_tp_pct%.
                             Sells partial_tp_ratio of position (e.g. 50%).
                             Banks quick wins on ranging days.
                             Set partial_tp_pct=0 to disable.
                             Only fires if partial_done=False.

    3. Trailing stop       — activates once price rises trailing_activation_pct%.
                             Trails trailing_stop_pct% below peak.
                             Handles the remainder after partial TP, or full
                             position if partial TP is disabled.

    4. Full take profit    — hard exit for remainder if take_profit_pct > 0.
                             Optional override — set to 0 to let trail handle it.

    Hybrid example (partial_tp_pct=0.8, trailing_activation_pct=1.0):
      Entry: $2200
      → Price hits $2217.6 (+0.8%) → sell 50%, bank $8
      → Price keeps running to $2374 (+7.9%)
      → Trail activates at $2222, trails 0.8% below peak
      → Price drops from $2374 → trail fires at ~$2355
      → Remaining 50% exits at +7% → ~$32 more
      Total: ~$40 vs $8 from pure TP exit ✅

    Returns:
        "stop_loss" | "partial_take_profit" | "take_profit" | "trailing_stop" | None
    """
    pnl_pct = ((current_price - entry_price) / entry_price) * 100
    peak_pct = ((peak_price - entry_price) / entry_price) * 100

    # 1. Hard stop loss — always checked first
    if pnl_pct <= -stop_loss_pct:
        logger.warning(f"Signal: STOP LOSS 🛑 | PnL={pnl_pct:.2f}%")
        return "stop_loss"

    # 2. Partial take profit — fires once at partial_tp_pct%
    if partial_tp_pct > 0 and not partial_done and pnl_pct >= partial_tp_pct:
        logger.info(
            f"Signal: PARTIAL TAKE PROFIT 💰 | PnL={pnl_pct:.2f}% | "
            f"target={partial_tp_pct}% | price=${current_price:.4f}"
        )
        return "partial_take_profit"

    # 3. Full take profit — hard exit if configured
    if take_profit_pct > 0 and pnl_pct >= take_profit_pct:
        logger.info(
            f"Signal: TAKE PROFIT 🎯 | PnL={pnl_pct:.2f}% | "
            f"target={take_profit_pct}% | price=${current_price:.4f}"
        )
        return "take_profit"

    # 4. Trailing stop — rides the remainder after partial TP
    trailing_active = peak_pct >= trailing_activation_pct
    if trailing_active:
        trailing_stop_level = peak_price * (1 - trailing_stop_pct / 100)
        drop_from_peak = ((peak_price - current_price) / peak_price) * 100

        logger.debug(
            f"Signal: trailing active | peak=${peak_price:.4f} (+{peak_pct:.2f}%) | "
            f"stop level=${trailing_stop_level:.4f} | "
            f"current=${current_price:.4f} | drop_from_peak={drop_from_peak:.2f}%"
        )

        if current_price <= trailing_stop_level:
            logger.info(
                f"Signal: TRAILING STOP 🎯 | "
                f"peak={peak_pct:.2f}% | locked_in≈{pnl_pct:.2f}% | "
                f"PnL=${((current_price - entry_price) * 1):.4f}"
            )
            return "trailing_stop"
    else:
        partial_note = " | partial TP done ✅" if partial_done else ""
        logger.debug(
            f"Signal: hold — PnL={pnl_pct:.2f}% | "
            f"partial_tp={partial_tp_pct}% | "
            f"trailing activates at +{trailing_activation_pct}% "
            f"(need +{trailing_activation_pct - pnl_pct:.2f}% more){partial_note}"
        )

    return None
