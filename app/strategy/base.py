"""
Strategy base — the pluggable strategy contract.

Every trading strategy implements the `Strategy` ABC. trader.py builds a
`MarketContext` each tick and asks the active strategy for an entry or exit
decision. The strategy owns its own config reads (via `self.settings`) and
its own gate logic — trader.py just builds context and acts on decisions.

The risk-layer skips (risk_halted / cooldown / sentiment) stay in trader.py;
those are not strategy concerns.

`hybrid_exit()` is the shared exit helper — the original signal_engine
`should_sell` logic, moved here verbatim so every strategy can reuse it.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from loguru import logger

from app.config import Settings

# ── Context objects ──────────────────────────────────────────────────────────

@dataclass
class MarketContext:
    """Indicator snapshot for one symbol at one tick. Built by trader.py."""
    symbol: str
    price: float
    rsi_15m: float
    rsi_1h: float
    ema_200: float
    ema_50: float
    ema_slope: float
    pullback_pct: float
    volume_ratio: float
    bb_squeeze: bool
    atr_pct: float


@dataclass
class PositionContext:
    """Open-position state for exit evaluation. Built by trader.py."""
    entry_price: float
    current_price: float
    peak_price: float
    partial_done: bool
    stop_loss_pct: float
    take_profit_pct: float


@dataclass
class EntryDecision:
    """Result of an entry evaluation."""
    should_enter: bool
    skip_reason: str | None = None  # set when should_enter is False


# ── Strategy ABC ─────────────────────────────────────────────────────────────

class Strategy(ABC):
    """
    Base class for all trading strategies.

    Subclasses set `name` (used for the STRATEGY env var and metric labels)
    and implement `evaluate_entry` / `evaluate_exit`.
    """

    #: short identifier — must match the key in signal_engine._STRATEGIES
    name: str = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def evaluate_entry(
        self, ctx: MarketContext, *, in_position: bool
    ) -> EntryDecision:
        """
        Decide whether to open a position.

        Returns an EntryDecision. When should_enter is False, skip_reason is
        a short label string (used directly as the bot_signal_skip_total
        Prometheus label).
        """

    @abstractmethod
    def evaluate_exit(self, pos: PositionContext) -> str | None:
        """
        Decide whether to close or reduce an open position.

        Returns one of:
          'stop_loss' | 'partial_take_profit' | 'take_profit' |
          'trailing_stop' | None
        """


# ── Shared exit helper ───────────────────────────────────────────────────────

def hybrid_exit(
    pos: PositionContext,
    *,
    trailing_activation_pct: float,
    trailing_stop_pct: float,
    partial_tp_pct: float,
) -> str | None:
    """
    Hybrid exit logic — four layers, checked in priority order:

    1. Hard stop loss      — fires if PnL <= -stop_loss_pct. Always active.
    2. Partial take profit — fires ONCE when PnL hits partial_tp_pct%.
                             Set partial_tp_pct=0 to disable.
    3. Full take profit    — hard exit if take_profit_pct > 0.
    4. Trailing stop       — activates at trailing_activation_pct%,
                             trails trailing_stop_pct% below peak.

    This is the original signal_engine.should_sell body, unchanged. Strategies
    call it from evaluate_exit with their own config values.

    Returns:
        "stop_loss" | "partial_take_profit" | "take_profit" |
        "trailing_stop" | None
    """
    pnl_pct = ((pos.current_price - pos.entry_price) / pos.entry_price) * 100
    peak_pct = ((pos.peak_price - pos.entry_price) / pos.entry_price) * 100

    # 1. Hard stop loss — always checked first
    if pnl_pct <= -pos.stop_loss_pct:
        logger.warning(f"Signal: STOP LOSS 🛑 | PnL={pnl_pct:.2f}%")
        return "stop_loss"

    # 2. Partial take profit — fires once at partial_tp_pct%
    if partial_tp_pct > 0 and not pos.partial_done and pnl_pct >= partial_tp_pct:
        logger.info(
            f"Signal: PARTIAL TAKE PROFIT 💰 | PnL={pnl_pct:.2f}% | "
            f"target={partial_tp_pct}% | price=${pos.current_price:.4f}"
        )
        return "partial_take_profit"

    # 3. Full take profit — hard exit if configured
    if pos.take_profit_pct > 0 and pnl_pct >= pos.take_profit_pct:
        logger.info(
            f"Signal: TAKE PROFIT 🎯 | PnL={pnl_pct:.2f}% | "
            f"target={pos.take_profit_pct}% | price=${pos.current_price:.4f}"
        )
        return "take_profit"

    # 4. Trailing stop — rides the remainder after partial TP
    trailing_active = peak_pct >= trailing_activation_pct
    if trailing_active:
        trailing_stop_level = pos.peak_price * (1 - trailing_stop_pct / 100)
        drop_from_peak = ((pos.peak_price - pos.current_price) / pos.peak_price) * 100

        logger.debug(
            f"Signal: trailing active | peak=${pos.peak_price:.4f} (+{peak_pct:.2f}%) | "
            f"stop level=${trailing_stop_level:.4f} | "
            f"current=${pos.current_price:.4f} | drop_from_peak={drop_from_peak:.2f}%"
        )

        if pos.current_price <= trailing_stop_level:
            logger.info(
                f"Signal: TRAILING STOP 🎯 | "
                f"peak={peak_pct:.2f}% | locked_in≈{pnl_pct:.2f}% | "
                f"PnL=${((pos.current_price - pos.entry_price) * 1):.4f}"
            )
            return "trailing_stop"
    else:
        partial_note = " | partial TP done ✅" if pos.partial_done else ""
        logger.debug(
            f"Signal: hold — PnL={pnl_pct:.2f}% | "
            f"partial_tp={partial_tp_pct}% | "
            f"trailing activates at +{trailing_activation_pct}% "
            f"(need +{trailing_activation_pct - pnl_pct:.2f}% more){partial_note}"
        )

    return None
