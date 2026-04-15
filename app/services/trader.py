"""
Trader — Main trading loop.
Supports multiple symbols concurrently. Each symbol has its own
isolated state (position, peak, cooldown, partial_done, etc).
"""

import time
import requests
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional

from loguru import logger

from app.config import settings
from app.exchange.binance_client import BinanceClient, BotExchangeError
from app.models.position import Position
from app.models.trade_event import TradeEvent
from app.services.coingecko import CoinGeckoSentiment
from app.services.notifier import Notifier
from app.strategy.indicators import (
    compute_ema,
    compute_ema_50,
    compute_ema_slope,
    compute_atr,
    compute_atr_stop_pct,
    compute_atr_take_profit_pct,
    compute_pullback_pct,
    compute_rsi,
    compute_volume_ratio,
    compute_bb_squeeze,
)
from app.strategy.risk_manager import RiskManager
from app.strategy.signal_engine import is_trend_bullish, should_buy, should_sell
from app.services.position_store import save_position, update_peak, clear_position, load_position

DASHBOARD_URL = "http://eth-dashboard:5000/api/trade"
DASHBOARD_SIGNAL_URL = "http://eth-dashboard:5000/api/signal"


@dataclass
class SymbolState:
    """All mutable per-symbol state isolated here."""
    symbol: str
    position: Optional[Position] = None
    peak_price: float = 0.0
    active_stop_loss_pct: float = field(default_factory=lambda: settings.stop_loss_pct)
    active_take_profit_pct: float = field(default_factory=lambda: settings.take_profit_pct)
    partial_done: bool = False
    last_stop_loss_time: float = 0.0
    consecutive_stop_losses: int = 0
    day_trades: list = field(default_factory=list)

    def cooldown_seconds(self) -> int:
        base = 900
        max_cd = 3600
        return min(base * max(1, self.consecutive_stop_losses), max_cd)

    def in_cooldown(self) -> bool:
        return (time.time() - self.last_stop_loss_time) < self.cooldown_seconds()

    def seconds_remaining_cooldown(self) -> int:
        return max(0, int(self.cooldown_seconds() - (time.time() - self.last_stop_loss_time)))


class Trader:
    def __init__(self):
        self.client = BinanceClient()
        self.notifier = Notifier()
        self.coingecko = CoinGeckoSentiment()
        self._today: date = datetime.utcnow().date()
        self.trade_log: list[TradeEvent] = []

        initial_value = self._fetch_initial_portfolio_value()
        self.risk = RiskManager(
            daily_loss_limit_usdc=settings.daily_loss_limit_usdc,
            trade_amount_usdc=settings.trade_amount_usdc,
            max_trades_per_day=settings.max_trades_per_day,
            portfolio_drawdown_pct=settings.portfolio_drawdown_pct,
            initial_portfolio_value=initial_value,
        )

        # Initialise per-symbol state
        self.states: dict[str, SymbolState] = {}
        for sym in settings.active_symbols:
            state = SymbolState(symbol=sym)
            position, peak_price, sl_pct, tp_pct = load_position(sym)
            state.position = position
            state.peak_price = peak_price
            state.active_stop_loss_pct = sl_pct if sl_pct > 0 else settings.stop_loss_pct
            state.active_take_profit_pct = tp_pct if tp_pct > 0 else settings.take_profit_pct
            self.states[sym] = state
            if position:
                logger.warning(
                    f"[{sym}] ⚠️  Resumed open position — "
                    f"entry=${position.entry_price} | peak=${peak_price} | "
                    f"SL={state.active_stop_loss_pct:.2f}%"
                )

    def _fetch_initial_portfolio_value(self) -> float:
        try:
            usdc = self.client.get_balance("USDC")
            # Use first symbol for price reference
            sym = settings.active_symbols[0]
            base = sym.replace("USDC", "").replace("USDT", "")
            base_bal = self.client.get_balance(base)
            price = self.client.get_price(sym)
            total = round(usdc + base_bal * price, 2)
            logger.info(f"RiskManager: initial portfolio value ${total:.2f}")
            return total
        except Exception as e:
            logger.warning(f"Could not fetch initial portfolio value: {e}")
            return 0.0

    def run(self) -> None:
        logger.info("=" * 60)
        logger.info(f"  ETH Bot starting | symbols={settings.active_symbols}")
        logger.info(f"  Testnet={settings.testnet} | SL={settings.stop_loss_pct}%")
        logger.info(
            f"  Trailing: activates at +{settings.trailing_activation_pct}% | "
            f"trails by {settings.trailing_stop_pct}% from peak"
        )
        logger.info(f"  ATR stop: enabled (multiplier={settings.atr_multiplier}x)")
        logger.info(f"  EMA slope min: {settings.ema_slope_min_pct}%")
        logger.info(f"  Max volume ratio: {settings.max_volume_ratio}x")
        logger.info(f"  1h RSI min: {settings.rsi_1h_min}")
        logger.info(
            f"  Partial TP: {settings.partial_tp_pct}% → sell {int(settings.partial_tp_ratio * 100)}% | "
            f"trail remainder"
        )
        logger.info("=" * 60)
        self.notifier.bot_started(settings.testnet)

        while True:
            try:
                self._maybe_reset_daily()
                for sym in settings.active_symbols:
                    self._tick(sym)
            except BotExchangeError as e:
                logger.error(f"Exchange error: {e} — will retry")
            except KeyboardInterrupt:
                logger.info("Bot stopped by user.")
                break
            except Exception as e:
                logger.exception(f"Unexpected error: {e}")
            time.sleep(settings.loop_interval_seconds)

    def _tick(self, symbol: str) -> None:
        state = self.states[symbol]

        price = self.client.get_price(symbol)
        candles_15m = self.client.get_candles(symbol, "15m", limit=250)
        candles_1h = self.client.get_candles(symbol, "1h", limit=250)

        closes_15m = candles_15m["close"]
        closes_1h = candles_1h["close"]
        highs_15m = candles_15m["high"]
        lows_15m = candles_15m["low"]
        volumes_15m = candles_15m["volume"]

        rsi = compute_rsi(closes_15m)
        rsi_1h = compute_rsi(closes_1h)
        ema_200 = compute_ema(closes_1h, 200)
        ema_50 = compute_ema_50(closes_1h)
        ema_slope = compute_ema_slope(closes_1h, period=200, lookback=5)
        pullback = compute_pullback_pct(closes_15m)
        volume_ratio = compute_volume_ratio(volumes_15m)
        bb_squeeze = compute_bb_squeeze(closes_15m)
        trend_ok = is_trend_bullish(price, ema_200)
        ema_50_above_200 = ema_50 > ema_200

        atr_raw = compute_atr(highs_15m, lows_15m, closes_15m, period=14)
        atr_pct = (atr_raw / price) * 100 if price > 0 else 0.0

        cg = self.coingecko.get_sentiment()
        cg_blocked = cg is not None and cg.should_block_trade(trend_bullish=trend_ok)
        cg_ok = not cg_blocked

        # Portfolio drawdown check
        if settings.portfolio_drawdown_pct > 0:
            try:
                usdc = self.client.get_balance("USDC")
                base = symbol.replace("USDC", "").replace("USDT", "")
                base_bal = self.client.get_balance(base)
                total_value = round(usdc + base_bal * price, 2)
                self.risk.update_portfolio_value(total_value)
            except Exception:
                pass

        # Update peak price while in position
        if state.position and price > state.peak_price:
            state.peak_price = price
            logger.debug(f"[{symbol}] New peak: ${state.peak_price:.4f}")
            update_peak(symbol, state.peak_price)

        # Signal log
        pnl_pct = None
        if state.position:
            pnl_pct = round(
                ((price - state.position.entry_price) / state.position.entry_price) * 100, 3
            )

        self._push_signal({
            "symbol": symbol,
            "timestamp": datetime.utcnow().strftime("%H:%M:%S"),
            "price": round(price, 4),
            "rsi_15m": round(rsi, 1),
            "rsi_1h": round(rsi_1h, 1),
            "ema_slope": round(ema_slope, 4),
            "pullback": round(pullback, 2),
            "vol_ratio": round(volume_ratio, 2),
            "atr_pct": round(atr_pct, 2),
            "trend": trend_ok,
            "ema_cross": ema_50_above_200,
            "squeeze": bb_squeeze,
            "sentiment": cg.sentiment if cg else "—",
            "position": state.position is not None,
            "pnl_pct": pnl_pct,
        })

        logger.debug(
            f"[{symbol}] price=${price:.4f} | 15m RSI={rsi:.1f} | 1h RSI={rsi_1h:.1f} | "
            f"pullback={pullback:.2f}% | trend={'✅' if trend_ok else '❌'} | "
            f"50>200={'✅' if ema_50_above_200 else '❌'} | "
            f"ema_slope={ema_slope:.4f}% | vol_ratio={volume_ratio:.2f}x | "
            f"atr={atr_pct:.2f}% | squeeze={'🔥' if bb_squeeze else '—'} | "
            f"cg={cg.sentiment if cg else 'unavailable'} | "
            f"peak=${state.peak_price:.4f} | "
            f"cooldown={'🕐 ' + str(state.seconds_remaining_cooldown()) + 's' if state.in_cooldown() else 'none'} | "
            f"position={'open' if state.position else 'none'}"
            f"{' | partial=done' if state.partial_done else ''}"
        )

        # ── Sell logic ────────────────────────────────────────────────────────
        if state.position:
            decision = should_sell(
                entry_price=state.position.entry_price,
                current_price=price,
                peak_price=state.peak_price,
                stop_loss_pct=state.active_stop_loss_pct,
                take_profit_pct=state.active_take_profit_pct,
                trailing_activation_pct=settings.trailing_activation_pct,
                trailing_stop_pct=settings.trailing_stop_pct,
                partial_tp_pct=settings.partial_tp_pct,
                partial_done=state.partial_done,
            )
            if decision == "partial_take_profit":
                self._execute_partial_sell(symbol, state, price, cg_summary=cg.summary if cg else None)
                return
            elif decision:
                self._execute_sell(symbol, state, price, decision, cg_summary=cg.summary if cg else None)
                return

        # ── Buy logic ─────────────────────────────────────────────────────────
        if not self.risk.can_trade():
            return

        if state.in_cooldown():
            logger.info(
                f"[{symbol}] Cooldown active — {state.seconds_remaining_cooldown()}s remaining "
                f"(consecutive SLs: {state.consecutive_stop_losses})"
            )
            return

        if not cg_ok:
            logger.info(
                f"[{symbol}] CoinGecko: skipping buy — EXTREME FEAR + price below 200 EMA | {cg.summary}"
            )
            return

        if should_buy(
            trend_bullish=trend_ok,
            ema_slope=ema_slope,
            rsi=rsi,
            rsi_1h=rsi_1h,
            pullback_pct=pullback,
            volume_ratio=volume_ratio,
            in_position=state.position is not None,
            ema_50_above_200=ema_50_above_200,
            bb_squeeze=bb_squeeze,
            rsi_oversold=settings.rsi_oversold,
            pullback_min_pct=settings.pullback_min_pct,
            ema_slope_min_pct=settings.ema_slope_min_pct,
            max_volume_ratio=settings.max_volume_ratio,
            rsi_1h_min=settings.rsi_1h_min,
        ):
            atr_stop = compute_atr_stop_pct(
                highs_15m, lows_15m, closes_15m,
                period=14,
                multiplier=settings.atr_multiplier,
            )
            state.active_stop_loss_pct = max(atr_stop, settings.stop_loss_pct)

            if settings.take_profit_pct > 0:
                state.active_take_profit_pct = settings.take_profit_pct
            else:
                state.active_take_profit_pct = compute_atr_take_profit_pct(
                    highs_15m, lows_15m, closes_15m,
                    rsi=rsi,
                    ema_slope=ema_slope,
                    period=14,
                    multiplier=settings.atr_tp_multiplier,
                    min_tp_pct=settings.atr_tp_min_pct,
                    max_tp_pct=settings.atr_tp_max_pct,
                )

            self._execute_buy(symbol, state, price, atr_pct=atr_pct, cg_summary=cg.summary if cg else None)

    def _get_balances(self, symbol: str) -> tuple[float, float, float]:
        try:
            usdc = self.client.get_balance("USDC")
            base = symbol.replace("USDC", "").replace("USDT", "")
            base_bal = self.client.get_balance(base)
            price = self.client.get_price(symbol)
            total = round(usdc + base_bal * price, 2)
            return round(usdc, 2), round(base_bal, 6), total
        except Exception as e:
            logger.warning(f"[{symbol}] Could not fetch balances: {e}")
            return 0.0, 0.0, 0.0

    def _execute_buy(
        self, symbol: str, state: SymbolState, price: float,
        atr_pct: float = 0.0, cg_summary: str | None = None
    ) -> None:
        usdc_amount = self.risk.position_size_usdc(atr_pct=atr_pct)
        order = self.client.place_market_buy(symbol, usdc_amount)
        qty = float(order.get("executedQty", 0))
        avg_price = float(order.get("cummulativeQuoteQty", usdc_amount)) / qty if qty else price

        state.position = Position(
            symbol=symbol,
            entry_price=avg_price,
            quantity=qty,
            order_id=str(order.get("orderId", "")),
        )
        state.peak_price = avg_price
        state.partial_done = False

        save_position(
            state.position,
            peak_price=state.peak_price,
            stop_loss_pct=state.active_stop_loss_pct,
            take_profit_pct=state.active_take_profit_pct,
        )

        event = TradeEvent(symbol=symbol, side="BUY", price=avg_price, quantity=qty, reason="signal")
        self.trade_log.append(event)

        bal_usdc, bal_base, total_val = self._get_balances(symbol)
        self.notifier.trade_opened(
            symbol, avg_price, qty, usdc_amount,
            cg_summary=cg_summary,
            balance_usdc=bal_usdc,
            balance_eth=bal_base,
            total_value=total_val,
        )
        self._push_to_dashboard({
            "symbol": symbol,
            "side": "BUY",
            "price": avg_price,
            "quantity": qty,
            "reason": "signal",
        })

        logger.info(
            f"[{symbol}] BUY executed @ ${avg_price:.4f} | qty={qty} | "
            f"SL={state.active_stop_loss_pct:.2f}% | "
            f"partial TP at +{settings.partial_tp_pct}% | "
            f"trail activates at +{settings.trailing_activation_pct}%"
        )

    def _execute_partial_sell(
        self, symbol: str, state: SymbolState, price: float,
        cg_summary: str | None = None
    ) -> None:
        if not state.position:
            return

        partial_qty = round(state.position.quantity * settings.partial_tp_ratio, 5)
        remaining_qty = round(state.position.quantity - partial_qty, 5)

        logger.info(
            f"[{symbol}] PARTIAL SELL: {partial_qty} ({int(settings.partial_tp_ratio * 100)}%) | "
            f"keeping {remaining_qty} for trail"
        )

        order = self.client.place_limit_sell_with_fallback(
            symbol, partial_qty, price,
            limit_buffer_pct=settings.limit_sell_buffer_pct,
            fallback_timeout_seconds=settings.limit_sell_timeout_seconds,
        )

        avg_price = (
            float(order.get("cummulativeQuoteQty", 0)) / partial_qty
            if partial_qty else price
        )

        partial_pnl_usdc = (avg_price - state.position.entry_price) * partial_qty
        partial_pnl_pct = (
            (avg_price - state.position.entry_price) / state.position.entry_price
        ) * 100
        peak_pct = (
            (state.peak_price - state.position.entry_price) / state.position.entry_price
        ) * 100

        state.position = Position(
            symbol=symbol,
            entry_price=state.position.entry_price,
            quantity=remaining_qty,
            order_id=state.position.order_id,
        )
        state.partial_done = True

        save_position(
            state.position,
            peak_price=state.peak_price,
            stop_loss_pct=state.active_stop_loss_pct,
            take_profit_pct=state.active_take_profit_pct,
        )

        self.risk.record_trade_result(partial_pnl_usdc)
        state.day_trades.append({"pnl_usdc": partial_pnl_usdc, "reason": "partial_take_profit"})

        bal_usdc, bal_base, total_val = self._get_balances(symbol)
        self.notifier.trade_closed(
            symbol, "partial_take_profit", partial_pnl_usdc, partial_pnl_pct,
            daily_pnl=self.risk._daily_pnl_usdc,
            cg_summary=cg_summary,
            balance_usdc=bal_usdc,
            balance_eth=bal_base,
            total_value=total_val,
            peak_pct=peak_pct,
        )
        self._push_to_dashboard({
            "symbol": symbol,
            "side": "SELL",
            "price": avg_price,
            "quantity": partial_qty,
            "reason": "partial_take_profit",
            "pnl_usdc": round(partial_pnl_usdc, 4),
            "pnl_pct": round(partial_pnl_pct, 4),
            "daily_pnl": round(self.risk._daily_pnl_usdc, 4),
            "peak_pct": round(peak_pct, 4),
        })

    def _execute_sell(
        self, symbol: str, state: SymbolState, price: float,
        reason: str, cg_summary: str | None = None
    ) -> None:
        if not state.position:
            return

        order = self.client.place_limit_sell_with_fallback(
            symbol, state.position.quantity, price,
            limit_buffer_pct=settings.limit_sell_buffer_pct,
            fallback_timeout_seconds=settings.limit_sell_timeout_seconds,
        )
        avg_price = (
            float(order.get("cummulativeQuoteQty", 0)) / state.position.quantity
            if state.position.quantity else price
        )

        pnl_usdc = state.position.pnl_usdc(avg_price)
        pnl_pct = state.position.pnl_pct(avg_price)
        peak_pct = (
            (state.peak_price - state.position.entry_price) / state.position.entry_price
        ) * 100

        if reason == "stop_loss":
            state.consecutive_stop_losses += 1
            state.last_stop_loss_time = time.time()
            cd = state.cooldown_seconds()
            logger.info(
                f"[{symbol}] Stop loss #{state.consecutive_stop_losses} — "
                f"cooldown {cd}s ({cd // 60} min)"
            )
        elif reason in ("trailing_stop", "take_profit"):
            state.consecutive_stop_losses = 0

        event = TradeEvent(symbol=symbol, side="SELL", price=avg_price,
                           quantity=state.position.quantity, reason=reason)
        self.trade_log.append(event)
        self.risk.record_trade_result(pnl_usdc)
        state.day_trades.append({"pnl_usdc": pnl_usdc, "reason": reason})

        bal_usdc, bal_base, total_val = self._get_balances(symbol)
        self.notifier.trade_closed(
            symbol, reason, pnl_usdc, pnl_pct,
            daily_pnl=self.risk._daily_pnl_usdc,
            cg_summary=cg_summary,
            balance_usdc=bal_usdc,
            balance_eth=bal_base,
            total_value=total_val,
            peak_pct=peak_pct,
        )
        self._push_to_dashboard({
            "symbol": symbol,
            "side": "SELL",
            "price": avg_price,
            "quantity": state.position.quantity,
            "reason": reason,
            "pnl_usdc": round(pnl_usdc, 4),
            "pnl_pct": round(pnl_pct, 4),
            "daily_pnl": round(self.risk._daily_pnl_usdc, 4),
            "peak_pct": round(peak_pct, 4),
        })

        if self.risk.is_halted:
            self.notifier.daily_halted(
                self.risk._daily_pnl_usdc,
                balance_usdc=bal_usdc,
                total_value=total_val,
            )

        state.peak_price = 0.0
        state.partial_done = False
        state.active_stop_loss_pct = settings.stop_loss_pct
        state.active_take_profit_pct = settings.take_profit_pct
        state.position = None
        clear_position(symbol)

    def _push_to_dashboard(self, trade: dict) -> None:
        try:
            requests.post(DASHBOARD_URL, json=trade, timeout=2)
        except Exception:
            pass

    def _push_signal(self, signal: dict) -> None:
        try:
            requests.post(DASHBOARD_SIGNAL_URL, json=signal, timeout=1)
        except Exception:
            pass

    def _maybe_reset_daily(self) -> None:
        today = datetime.utcnow().date()
        if today != self._today:
            self._send_daily_summary()
            logger.info(f"New trading day: {today}")
            self.risk.reset_daily()
            for state in self.states.values():
                state.day_trades = []
            self._today = today

    def _send_daily_summary(self) -> None:
        try:
            date_str = self._today.isoformat()
            # Aggregate across all symbols
            all_trades = []
            for state in self.states.values():
                all_trades.extend(state.day_trades)

            closed = [t for t in all_trades if t.get("pnl_usdc") is not None]
            trades_count = len(closed)
            wins = sum(1 for t in closed if t["pnl_usdc"] > 0)
            losses = sum(1 for t in closed if t["pnl_usdc"] <= 0)
            daily_pnl = sum(t["pnl_usdc"] for t in closed)
            best = max((t["pnl_usdc"] for t in closed), default=None)
            worst = min((t["pnl_usdc"] for t in closed), default=None)

            # Use first symbol for balance fetch
            sym = settings.active_symbols[0]
            bal_usdc, _, total_val = self._get_balances(sym)

            self.notifier.daily_summary(
                date_str=date_str,
                daily_pnl=daily_pnl,
                trades_count=trades_count,
                wins=wins,
                losses=losses,
                balance_usdc=bal_usdc,
                total_value=total_val,
                best_trade=best,
                worst_trade=worst,
            )
        except Exception as e:
            logger.warning(f"Could not send daily summary: {e}")
