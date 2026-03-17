"""
Signal Engine
─────────────
Decides WHEN to buy and WHEN to sell based on indicator readings.
All logic is pure functions — easy to test, no side effects.

Entry strategy (v1):
  - Trend filter : price is above 200 EMA on the same timeframe
  - Pullback     : RSI(14) < rsi_oversold AND price is X% below recent high
  - One position at a time

Exit strategy:
  - Take profit  : PnL >= take_profit_pct
  - Stop loss    : PnL <= -stop_loss_pct
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

    Args:
        trend_bullish:   Is price above 200 EMA?
        rsi:             Current RSI(14) value on the entry timeframe.
        pullback_pct:    How far (%) price has dropped from its recent high.
        in_position:     Is there already an open trade?
        rsi_oversold:    RSI threshold below which we consider the dip valid.
        pullback_min_pct: Minimum pullback % required before entry.
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

    logger.info(
        f"Signal: BUY ✅ | RSI={rsi:.1f} | pullback={pullback_pct:.2f}% | trend_ok=True"
    )
    return True


def should_sell(
    *,
    entry_price: float,
    current_price: float,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> str | None:
    """
    Returns the exit reason string, or None if we should hold.

    Returns:
        "take_profit" | "stop_loss" | None
    """
    pnl_pct = ((current_price - entry_price) / entry_price) * 100

    if pnl_pct <= -stop_loss_pct:
        logger.warning(f"Signal: STOP LOSS 🛑 | PnL={pnl_pct:.2f}%")
        return "stop_loss"

    if pnl_pct >= take_profit_pct:
        logger.info(f"Signal: TAKE PROFIT 💰 | PnL={pnl_pct:.2f}%")
        return "take_profit"

    logger.debug(f"Signal: hold — PnL={pnl_pct:.2f}%")
    return None
