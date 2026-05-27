"""
Signal Engine
─────────────
Primary role (post-refactor): strategy dispatcher.

  `get_strategy()` loads the active Strategy subclass selected by
  `settings.strategy`. Each strategy owns its own entry and exit logic;
  trader.py calls `strategy.evaluate_entry(ctx, in_position=...)` and
  `strategy.evaluate_exit(pos)` directly.

Compatibility layer:

  The legacy pure functions (`should_buy`, `should_sell`,
  `is_ema_slope_bullish`) are kept here byte-for-byte during the
  trader.py migration. Removing them before trader.py stops calling
  them would crash the bot. They will be deleted in a follow-up commit
  once trader.py is fully on the Strategy API.

  `is_trend_bullish` stays permanently — it's used by trader.py for
  non-strategy concerns (signal log entries, CoinGecko gating).

To add a new strategy:
  1. Create `app/strategy/<name>.py` with a `Strategy` subclass.
  2. Register it in `_STRATEGIES` below.
  3. Set `STRATEGY=<name>` in `.env`.
"""

from loguru import logger

from app.config import settings
from app.strategy.active_mean_rev import ActiveMeanRevStrategy
from app.strategy.base import Strategy
from app.strategy.pullback import PullbackStrategy

# ─────────────────────────────────────────────────────────────────────────────
# Strategy registry — the dispatcher
# ─────────────────────────────────────────────────────────────────────────────

_STRATEGIES: dict[str, type[Strategy]] = {
    "pullback": PullbackStrategy,
    "active_mean_rev": ActiveMeanRevStrategy,
}


def get_strategy() -> Strategy:
    """Return an instance of the strategy named by `settings.strategy`."""
    cls = _STRATEGIES.get(settings.strategy)
    if cls is None:
        raise ValueError(
            f"Unknown strategy '{settings.strategy}'. "
            f"Available: {sorted(_STRATEGIES)}"
        )
    logger.info(f"Strategy loaded: {settings.strategy}")
    return cls(settings)


# ─────────────────────────────────────────────────────────────────────────────
# Shared market helper — stays permanently
# ─────────────────────────────────────────────────────────────────────────────


def is_trend_bullish(current_price: float, ema_200: float) -> bool:
    """Macro trend filter: only buy when price is above the 200 EMA.

    Used by trader.py for non-strategy concerns (CoinGecko gating, signal
    log entries). Strategies derive their own trend booleans internally.
    """
    return current_price > ema_200


# ─────────────────────────────────────────────────────────────────────────────
# Legacy pure-function API — kept working during the strategy refactor.
# These will be removed once trader.py is fully migrated to the Strategy API.
# Do not extend or modify; new logic goes in a Strategy subclass.
# ─────────────────────────────────────────────────────────────────────────────


def is_ema_slope_bullish(ema_slope: float, min_slope_pct: float = 0.0) -> bool:
    """
    Returns True if the 200 EMA is rising (slope > min_slope_pct).
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
    Legacy entry decision — preserved during trader.py migration.

    See PullbackStrategy.evaluate_entry for the canonical version. The
    gate order and boundary conditions here are the original ones; the
    new strategy fixes a minor metric-labelling discrepancy (rsi_1h vs
    rsi_15m ordering) that does not affect this function's True/False
    output.
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
    Legacy exit decision — preserved during trader.py migration.

    See base.hybrid_exit for the canonical version (this function's body
    was moved there verbatim).

    Returns:
        "stop_loss" | "partial_take_profit" | "take_profit" |
        "trailing_stop" | None
    """
    pnl_pct = ((current_price - entry_price) / entry_price) * 100
    peak_pct = ((peak_price - entry_price) / entry_price) * 100

    if pnl_pct <= -stop_loss_pct:
        logger.warning(f"Signal: STOP LOSS 🛑 | PnL={pnl_pct:.2f}%")
        return "stop_loss"

    if partial_tp_pct > 0 and not partial_done and pnl_pct >= partial_tp_pct:
        logger.info(
            f"Signal: PARTIAL TAKE PROFIT 💰 | PnL={pnl_pct:.2f}% | "
            f"target={partial_tp_pct}% | price=${current_price:.4f}"
        )
        return "partial_take_profit"

    if take_profit_pct > 0 and pnl_pct >= take_profit_pct:
        logger.info(
            f"Signal: TAKE PROFIT 🎯 | PnL={pnl_pct:.2f}% | "
            f"target={take_profit_pct}% | price=${current_price:.4f}"
        )
        return "take_profit"

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
