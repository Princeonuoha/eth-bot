"""
ActiveMeanRevStrategy — looser mean-reversion entries for testnet / infra testing.

This is intentionally NOT a sophisticated trading strategy. It exists to:

  1. Demonstrate the Strategy ABC supports plugging in new entry logic
     with zero changes to trader.py.
  2. Fire trades often enough on testnet to generate real data for the
     observability stack — bot_trades_total, bot_pnl_usdc,
     bot_position_open transitions, bot_sl_order_active toggles.

Compared to PullbackStrategy it drops every bullish-regime filter (trend,
ema_cross, ema_slope, rsi_1h) and loosens the rest. Combined with
DISABLE_SENTIMENT_BLOCK=true on testnet, it should fire on the order of
10-30 trades/day across three pairs.

Entry gates (in order):
  1. in_position        — defensive, never add to an open position
  2. rsi_15m oversold   — 15m RSI < active_rsi_oversold (default 50)
  3. pullback           — pullback_pct >= active_pullback_min_pct (default 0.3%)
  4. volume             — volume_ratio < active_max_volume_ratio (default 2.0)

Exit is hybrid_exit driven by the active_* trailing/partial config values
(tighter than pullback's so positions cycle faster). The stop loss percent
at entry remains driven by global settings.stop_loss_pct — making SL
per-strategy is a separate architectural change.
"""

from loguru import logger

from app.strategy.base import (
    EntryDecision,
    MarketContext,
    PositionContext,
    Strategy,
    hybrid_exit,
)


class ActiveMeanRevStrategy(Strategy):
    """Loose 15m RSI mean reversion. Fires often. For testnet / infra testing."""

    name = "active_mean_rev"

    def evaluate_entry(
        self, ctx: MarketContext, *, in_position: bool
    ) -> EntryDecision:
        settings = self.settings

        # 1. Never add to an open position
        if in_position:
            return EntryDecision(False, "position_open")

        # 2. 15m RSI below (loose) oversold threshold
        if ctx.rsi_15m >= settings.active_rsi_oversold:
            logger.debug(
                f"[{ctx.symbol}] Signal: skip — 15m RSI {ctx.rsi_15m:.1f} "
                f"not below active threshold ({settings.active_rsi_oversold})"
            )
            return EntryDecision(False, "rsi_15m")

        # 3. Any meaningful pullback
        if ctx.pullback_pct < settings.active_pullback_min_pct:
            logger.debug(
                f"[{ctx.symbol}] Signal: skip — pullback {ctx.pullback_pct:.2f}% "
                f"below active min ({settings.active_pullback_min_pct}%)"
            )
            return EntryDecision(False, "pullback")

        # 4. Avoid panic-volume candles
        if ctx.volume_ratio >= settings.active_max_volume_ratio:
            logger.debug(
                f"[{ctx.symbol}] Signal: skip — volume spike "
                f"{ctx.volume_ratio:.2f}x avg (active max "
                f"{settings.active_max_volume_ratio}x)"
            )
            return EntryDecision(False, "volume")

        # All gates passed
        logger.info(
            f"[{ctx.symbol}] Signal: BUY ✅ [active_mean_rev] | "
            f"15m RSI={ctx.rsi_15m:.1f} | pullback={ctx.pullback_pct:.2f}% | "
            f"vol={ctx.volume_ratio:.2f}x"
        )
        return EntryDecision(True)

    def evaluate_exit(self, pos: PositionContext) -> str | None:
        return hybrid_exit(
            pos,
            trailing_activation_pct=self.settings.active_trailing_activation_pct,
            trailing_stop_pct=self.settings.active_trailing_stop_pct,
            partial_tp_pct=self.settings.active_partial_tp_pct,
        )
