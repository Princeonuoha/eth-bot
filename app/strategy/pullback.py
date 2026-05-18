"""
PullbackStrategy — the existing pullback mean-reversion entry strategy.

This is a behaviour-preserving move of the logic that used to live in
`signal_engine.should_buy` / `should_sell`. Trading decisions are unchanged
when STRATEGY=pullback (the default).

One intentional correction: the gate order in `evaluate_entry` now matches
`should_buy` exactly (rsi_1h is checked *before* rsi_15m). The previous
`_first_skip_reason` in trader.py had these swapped, which meant the
bot_signal_skip_total metric occasionally blamed a different gate than
the one that actually stopped the trade. Now there is a single source of
truth — the same code path that returns the decision returns the skip reason.

Entry gates (in priority order, matches the original should_buy):
  1. in_position             — defensive, never add to a position
  2. trend                   — price must be above 200 EMA (1h)
  3. ema_cross               — 50 EMA must be above 200 EMA (1h)
  4. ema_slope               — 200 EMA must be rising (strict > threshold)
  5. rsi_1h                  — 1h RSI must not be bearish
  6. rsi_15m                 — 15m RSI must be oversold
  7. pullback                — % drop from recent high must meet symbol minimum
  8. volume                  — current volume must not be a panic spike

Exit is delegated to `hybrid_exit()` in base.py — same four-layer logic
(stop loss → partial TP → full TP → trailing stop) as the original
`should_sell`.
"""

from loguru import logger

from app.strategy.base import (
    EntryDecision,
    MarketContext,
    PositionContext,
    Strategy,
    hybrid_exit,
)


class PullbackStrategy(Strategy):
    """Mean-reversion entries on confirmed uptrends."""

    name = "pullback"

    def evaluate_entry(
        self, ctx: MarketContext, *, in_position: bool
    ) -> EntryDecision:
        settings = self.settings

        # 1. Never add to an open position
        if in_position:
            logger.debug(f"[{ctx.symbol}] Signal: skip — already in position")
            return EntryDecision(False, "position_open")

        # 2. Macro trend filter — price must be above the 200 EMA
        if not (ctx.price > ctx.ema_200):
            logger.debug(f"[{ctx.symbol}] Signal: skip — price below 200 EMA")
            return EntryDecision(False, "no_uptrend")

        # 3. Golden cross zone — 50 EMA above 200 EMA
        if not (ctx.ema_50 > ctx.ema_200):
            logger.debug(
                f"[{ctx.symbol}] Signal: skip — 50 EMA below 200 EMA (no golden cross)"
            )
            return EntryDecision(False, "ema_cross")

        # 4. EMA slope must be rising (strict >, matches original is_ema_slope_bullish)
        if not (ctx.ema_slope > settings.ema_slope_min_pct):
            logger.debug(
                f"[{ctx.symbol}] Signal: skip — EMA slope {ctx.ema_slope:.4f}% "
                f"below min {settings.ema_slope_min_pct}% (downtrend protection)"
            )
            return EntryDecision(False, "ema_slope")

        # 5. 1h RSI must not be bearish — checked BEFORE 15m RSI (matches should_buy)
        if ctx.rsi_1h < settings.rsi_1h_min:
            logger.debug(
                f"[{ctx.symbol}] Signal: skip — 1h RSI {ctx.rsi_1h:.1f} below "
                f"min {settings.rsi_1h_min} (higher timeframe bearish)"
            )
            return EntryDecision(False, "rsi_1h")

        # 6. 15m RSI must be oversold
        if ctx.rsi_15m >= settings.rsi_oversold:
            logger.debug(
                f"[{ctx.symbol}] Signal: skip — 15m RSI {ctx.rsi_15m:.1f} "
                f"not oversold (threshold {settings.rsi_oversold})"
            )
            return EntryDecision(False, "rsi_15m")

        # 7. Pullback must meet per-symbol minimum
        pullback_min = settings.pullback_for(ctx.symbol)
        if ctx.pullback_pct < pullback_min:
            logger.debug(
                f"[{ctx.symbol}] Signal: skip — pullback {ctx.pullback_pct:.2f}% "
                f"too small (min {pullback_min}%)"
            )
            return EntryDecision(False, "pullback")

        # 8. Volume must not be a panic spike (strict >=, matches should_buy)
        if ctx.volume_ratio >= settings.max_volume_ratio:
            logger.debug(
                f"[{ctx.symbol}] Signal: skip — volume spike {ctx.volume_ratio:.2f}x "
                f"avg (max {settings.max_volume_ratio}x) — possible panic candle"
            )
            return EntryDecision(False, "volume")

        # All gates passed
        squeeze_note = " | BB squeeze 🔥" if ctx.bb_squeeze else ""
        logger.info(
            f"[{ctx.symbol}] Signal: BUY ✅ | 15m RSI={ctx.rsi_15m:.1f} | "
            f"1h RSI={ctx.rsi_1h:.1f} | pullback={ctx.pullback_pct:.2f}% | "
            f"ema_slope={ctx.ema_slope:.4f}% | 50>200=✅ | "
            f"vol={ctx.volume_ratio:.2f}x{squeeze_note}"
        )
        return EntryDecision(True)

    def evaluate_exit(self, pos: PositionContext) -> str | None:
        return hybrid_exit(
            pos,
            trailing_activation_pct=self.settings.trailing_activation_pct,
            trailing_stop_pct=self.settings.trailing_stop_pct,
            partial_tp_pct=self.settings.partial_tp_pct,
        )
