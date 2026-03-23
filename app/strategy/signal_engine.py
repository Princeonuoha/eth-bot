"""
Signal Engine
─────────────
Decides WHEN to buy and WHEN to sell based on indicator readings.
All logic is pure functions — easy to test, no side effects.

Entry strategy:
  - Trend filter : price is above 200 EMA on the 1h timeframe
  - Pullback     : RSI(14) < rsi_oversold AND price is X% below recent high
  - One position at a time

Exit strategy (trailing stop):
  - Hard stop loss : PnL <= -stop_loss_pct (protects against big drops)
  - Trailing stop  : once price rises >= trailing_activation_pct above entry,
                     a trailing stop activates that tracks the peak price.
                     If price drops trailing_stop_pct% below the peak → exit.
  - This lets the bot ride a bullish move and exit near the top
    rather than at a fixed take-profit target.
"""

from loguru import logger


def is_trend_bullish(current_price: float, ema_200: float) -> bool:
    """Macro trend filter: only buy when price is above the 200 EMA."""
    return current_price > ema_200


def should_buy(
    *,
    trend_bullish: bool,
    rsi: float,
    pullback_pct: float,
    in_position: bool,
    rsi_oversold: float,
    pullback_min_pct: float,
) -> bool:
    """
    Returns True if all entry conditions are met.
    """
    if in_position:
        logger.debug("Signal: skip — already in position")
        return False

    if not trend_bullish:
        logger.debug("Signal: skip — trend is not bullish (price below 200 EMA)")
        return False

    if rsi >= rsi_oversold:
        logger.debug(f"Signal: skip — RSI {rsi:.1f} not oversold (threshold {rsi_oversold})")
        return False

    if pullback_pct < pullback_min_pct:
        logger.debug(
            f"Signal: skip — pullback {pullback_pct:.2f}% too small (min {pullback_min_pct}%)"
        )
        return False

    logger.info(f"Signal: BUY ✅ | RSI={rsi:.1f} | pullback={pullback_pct:.2f}% | trend_ok=True")
    return True


def should_sell(
    *,
    entry_price: float,
    current_price: float,
    peak_price: float,
    stop_loss_pct: float,
    trailing_activation_pct: float,
    trailing_stop_pct: float,
) -> str | None:
    """
    Trailing stop exit logic. Returns exit reason or None to hold.

    How it works:
    1. Hard stop loss fires immediately if price drops stop_loss_pct% below entry.
       This is the safety net for sudden crashes.

    2. Trailing stop activates once price rises trailing_activation_pct% above entry.
       Once active, it tracks the highest price seen (peak_price).
       If price drops trailing_stop_pct% below that peak → exit with "trailing_stop".

    Example with trailing_activation_pct=1.0, trailing_stop_pct=0.8:
      - Entry: $2000
      - Hard stop: $1970 (-1.5%)
      - Trailing activates at: $2020 (+1%)
      - ETH runs to $2200 (peak)
      - Trailing stop level: $2200 * (1 - 0.008) = $2182.40
      - If price drops to $2182 → exit, locking in ~9% profit
      - Without trailing stop, you'd have exited at $2020 (+1%) and missed the run

    Returns:
        "stop_loss" | "trailing_stop" | None
    """
    pnl_pct = ((current_price - entry_price) / entry_price) * 100
    peak_pct = ((peak_price - entry_price) / entry_price) * 100

    # 1. Hard stop loss — always active, fires first
    if pnl_pct <= -stop_loss_pct:
        logger.warning(f"Signal: STOP LOSS 🛑 | PnL={pnl_pct:.2f}%")
        return "stop_loss"

    # 2. Trailing stop — only activates after price rises enough
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
        logger.debug(
            f"Signal: hold — PnL={pnl_pct:.2f}% | "
            f"trailing activates at +{trailing_activation_pct}% "
            f"(need +{trailing_activation_pct - pnl_pct:.2f}% more)"
        )

    return None
