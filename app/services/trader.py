"""
Trader — Main trading loop.
"""

import time
import requests
from datetime import datetime, date

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


class Trader:
    def __init__(self):
        self.client = BinanceClient()

        initial_value = self._fetch_initial_portfolio_value()

        self.risk = RiskManager(
            daily_loss_limit_usdc=settings.daily_loss_limit_usdc,
            trade_amount_usdc=settings.trade_amount_usdc,
            max_trades_per_day=settings.max_trades_per_day,
            portfolio_drawdown_pct=settings.portfolio_drawdown_pct,
            initial_portfolio_value=initial_value,
        )
        self.notifier = Notifier()
        self.coingecko = CoinGeckoSentiment()
        self._today: date = datetime.utcnow().date()
        self.trade_log: list[TradeEvent] = []

        self._last_stop_loss_time: float = 0
        self._consecutive_stop_losses: int = 0
        self._partial_done: bool = False

        # Daily stats for summary
        self._day_trades: list[dict] = []

        position, peak_price, sl_pct, tp_pct = load_position()

        self.position: Position | None = position
        self._peak_price: float = peak_price
        self._active_stop_loss_pct: float = sl_pct if sl_pct > 0 else settings.stop_loss_pct
        self._active_take_profit_pct: float = tp_pct if tp_pct > 0 else settings.take_profit_pct

        if position:
            logger.warning(
                f"⚠️  Resumed open position from DB — "
                f"entry=${position.entry_price} | peak=${peak_price} | "
                f"SL={self._active_stop_loss_pct:.2f}% | TP={self._active_take_profit_pct:.2f}%"
            )

    def _fetch_initial_portfolio_value(self) -> float:
        try:
            usdc = self.client.get_balance("USDC")
            eth = self.client.get_balance("ETH")
            price = self.client.get_price(settings.symbol)
            total = round(usdc + eth * price, 2)
            logger.info(f"RiskManager: initial portfolio value ${total:.2f}")
            return total
        except Exception as e:
            logger.warning(f"Could not fetch initial portfolio value: {e}")
            return 0.0

    def _cooldown_seconds(self) -> int:
        base = 900
        max_cd = 3600
        return min(base * max(1, self._consecutive_stop_losses), max_cd)

    def run(self) -> None:
        logger.info("=" * 60)
        logger.info(f"  ETH Bot starting | symbol={settings.symbol}")
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
        logger.info(
            f"  Max trades/day: {settings.max_trades_per_day or 'unlimited'} | "
            f"Portfolio drawdown halt: "
            f"{settings.portfolio_drawdown_pct}%" if settings.portfolio_drawdown_pct > 0
            else f"  Max trades/day: {settings.max_trades_per_day or 'unlimited'} | "
            f"Portfolio drawdown halt: disabled"
        )
        logger.info("=" * 60)
        self.notifier.bot_started(settings.testnet)

        while True:
            try:
                self._maybe_reset_daily()
                self._tick()
            except BotExchangeError as e:
                logger.error(f"Exchange error: {e} — will retry")
            except KeyboardInterrupt:
                logger.info("Bot stopped by user.")
                break
            except Exception as e:
                logger.exception(f"Unexpected error: {e}")
            time.sleep(settings.loop_interval_seconds)

    def _tick(self) -> None:
        symbol = settings.symbol

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

        cooldown = self._cooldown_seconds()
        seconds_since_sl = time.time() - self._last_stop_loss_time
        in_cooldown = seconds_since_sl < cooldown

        # Portfolio drawdown check
        if settings.portfolio_drawdown_pct > 0:
            try:
                usdc = self.client.get_balance("USDC")
                eth = self.client.get_balance("ETH")
                total_value = round(usdc + eth * price, 2)
                self.risk.update_portfolio_value(total_value)
            except Exception:
                pass

        # Update peak price while in position
        if self.position and price > self._peak_price:
            self._peak_price = price
            logger.debug(f"New peak: ${self._peak_price:.4f}")
            update_peak(self._peak_price)

        # ── Signal log — push to dashboard every tick ─────────────────────────
        pnl_pct = None
        if self.position:
            pnl_pct = round(((price - self.position.entry_price) / self.position.entry_price) * 100, 3)

        self._push_signal({
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
            "position": self.position is not None,
            "pnl_pct": pnl_pct,
        })

        logger.debug(
            f"price=${price:.4f} | 15m RSI={rsi:.1f} | 1h RSI={rsi_1h:.1f} | "
            f"pullback={pullback:.2f}% | trend={'✅' if trend_ok else '❌'} | "
            f"50>200={'✅' if ema_50_above_200 else '❌'} | "
            f"ema_slope={ema_slope:.4f}% | vol_ratio={volume_ratio:.2f}x | "
            f"atr={atr_pct:.2f}% | squeeze={'🔥' if bb_squeeze else '—'} | "
            f"cg={cg.sentiment if cg else 'unavailable'}"
            f"{'⚠️ EXTREME FEAR' if cg and cg.is_extreme_fear() else ''} | "
            f"peak=${self._peak_price:.4f} | "
            f"cooldown={'🕐 ' + str(int(cooldown - seconds_since_sl)) + 's' if in_cooldown else 'none'} | "
            f"position={'open' if self.position else 'none'}"
            f"{' | partial=done' if self._partial_done else ''}"
        )

        if self.position:
            decision = should_sell(
                entry_price=self.position.entry_price,
                current_price=price,
                peak_price=self._peak_price,
                stop_loss_pct=self._active_stop_loss_pct,
                take_profit_pct=self._active_take_profit_pct,
                trailing_activation_pct=settings.trailing_activation_pct,
                trailing_stop_pct=settings.trailing_stop_pct,
                partial_tp_pct=settings.partial_tp_pct,
                partial_done=self._partial_done,
            )
            if decision == "partial_take_profit":
                self._execute_partial_sell(price, cg_summary=cg.summary if cg else None)
                return
            elif decision:
                self._execute_sell(price, decision, cg_summary=cg.summary if cg else None)
                return

        if not self.risk.can_trade():
            return

        if in_cooldown:
            logger.info(
                f"Cooldown active — {int(cooldown - seconds_since_sl)}s remaining "
                f"(consecutive SLs: {self._consecutive_stop_losses})"
            )
            return

        if not cg_ok:
            logger.info(
                f"CoinGecko: skipping buy — EXTREME FEAR + price below 200 EMA | {cg.summary}"
            )
            return

        if should_buy(
            trend_bullish=trend_ok,
            ema_slope=ema_slope,
            rsi=rsi,
            rsi_1h=rsi_1h,
            pullback_pct=pullback,
            volume_ratio=volume_ratio,
            in_position=self.position is not None,
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
            self._active_stop_loss_pct = max(atr_stop, settings.stop_loss_pct)

            if settings.take_profit_pct > 0:
                self._active_take_profit_pct = settings.take_profit_pct
                logger.info(
                    f"Take profit: FIXED {self._active_take_profit_pct:.2f}% "
                    f"(override from config)"
                )
            else:
                self._active_take_profit_pct = compute_atr_take_profit_pct(
                    highs_15m, lows_15m, closes_15m,
                    rsi=rsi,
                    ema_slope=ema_slope,
                    period=14,
                    multiplier=settings.atr_tp_multiplier,
                    min_tp_pct=settings.atr_tp_min_pct,
                    max_tp_pct=settings.atr_tp_max_pct,
                )
                logger.info(
                    f"Take profit: ATR-DYNAMIC {self._active_take_profit_pct:.2f}% "
                    f"(atr_mult={settings.atr_tp_multiplier}x | "
                    f"rsi={rsi:.1f} | ema_slope={ema_slope:.4f}%)"
                )

            logger.info(
                f"Entry plan: SL={self._active_stop_loss_pct:.2f}% | "
                f"partial_TP={settings.partial_tp_pct}% ({int(settings.partial_tp_ratio * 100)}%) | "
                f"trail_activation=+{settings.trailing_activation_pct}% | "
                f"ATR={atr_pct:.2f}% | "
                f"Risk:Reward = 1:{self._active_take_profit_pct / self._active_stop_loss_pct:.1f}"
            )
            self._execute_buy(price, atr_pct=atr_pct, cg_summary=cg.summary if cg else None)

    def _get_balances(self) -> tuple[float, float, float]:
        try:
            usdc = self.client.get_balance("USDC")
            eth = self.client.get_balance("ETH")
            price = self.client.get_price(settings.symbol)
            total = round(usdc + eth * price, 2)
            return round(usdc, 2), round(eth, 6), total
        except Exception as e:
            logger.warning(f"Could not fetch balances: {e}")
            return 0.0, 0.0, 0.0

    def _execute_buy(
        self, price: float, atr_pct: float = 0.0, cg_summary: str | None = None
    ) -> None:
        usdc_amount = self.risk.position_size_usdc(atr_pct=atr_pct)
        order = self.client.place_market_buy(settings.symbol, usdc_amount)
        qty = float(order.get("executedQty", 0))
        avg_price = float(order.get("cummulativeQuoteQty", usdc_amount)) / qty if qty else price

        self.position = Position(
            symbol=settings.symbol,
            entry_price=avg_price,
            quantity=qty,
            order_id=str(order.get("orderId", "")),
        )

        self._peak_price = avg_price
        self._partial_done = False

        save_position(
            self.position,
            peak_price=self._peak_price,
            stop_loss_pct=self._active_stop_loss_pct,
            take_profit_pct=self._active_take_profit_pct,
        )

        event = TradeEvent(
            symbol=settings.symbol, side="BUY",
            price=avg_price, quantity=qty, reason="signal"
        )
        self.trade_log.append(event)

        bal_usdc, bal_eth, total_val = self._get_balances()
        self.notifier.trade_opened(
            settings.symbol, avg_price, qty, usdc_amount,
            cg_summary=cg_summary,
            balance_usdc=bal_usdc,
            balance_eth=bal_eth,
            total_value=total_val,
        )
        self._push_to_dashboard({
            "side": "BUY",
            "price": avg_price,
            "quantity": qty,
            "reason": "signal",
        })

        logger.info(
            f"Trailing stop: activates at "
            f"${avg_price * (1 + settings.trailing_activation_pct / 100):.4f} "
            f"(+{settings.trailing_activation_pct}%) | "
            f"hard stop at ${avg_price * (1 - self._active_stop_loss_pct / 100):.4f} "
            f"(-{self._active_stop_loss_pct:.2f}%) | "
            f"partial TP at +{settings.partial_tp_pct}% "
            f"(sell {int(settings.partial_tp_ratio * 100)}%)"
        )

    def _execute_partial_sell(self, price: float, cg_summary: str | None = None) -> None:
        if not self.position:
            return

        partial_qty = round(self.position.quantity * settings.partial_tp_ratio, 5)
        remaining_qty = round(self.position.quantity - partial_qty, 5)

        logger.info(
            f"PARTIAL SELL: {partial_qty} ETH ({int(settings.partial_tp_ratio * 100)}%) | "
            f"keeping {remaining_qty} ETH for trail"
        )

        order = self.client.place_limit_sell_with_fallback(
            settings.symbol,
            partial_qty,
            price,
            limit_buffer_pct=settings.limit_sell_buffer_pct,
            fallback_timeout_seconds=settings.limit_sell_timeout_seconds,
        )

        avg_price = (
            float(order.get("cummulativeQuoteQty", 0)) / partial_qty
            if partial_qty else price
        )

        partial_pnl_usdc = (avg_price - self.position.entry_price) * partial_qty
        partial_pnl_pct = (
            (avg_price - self.position.entry_price) / self.position.entry_price
        ) * 100
        peak_pct = (
            (self._peak_price - self.position.entry_price) / self.position.entry_price
        ) * 100

        self.position = Position(
            symbol=self.position.symbol,
            entry_price=self.position.entry_price,
            quantity=remaining_qty,
            order_id=self.position.order_id,
        )
        self._partial_done = True

        save_position(
            self.position,
            peak_price=self._peak_price,
            stop_loss_pct=self._active_stop_loss_pct,
            take_profit_pct=self._active_take_profit_pct,
        )

        self.risk.record_trade_result(partial_pnl_usdc)
        self._day_trades.append({"pnl_usdc": partial_pnl_usdc, "reason": "partial_take_profit"})

        bal_usdc, bal_eth, total_val = self._get_balances()

        self.notifier.trade_closed(
            settings.symbol, "partial_take_profit", partial_pnl_usdc, partial_pnl_pct,
            daily_pnl=self.risk._daily_pnl_usdc,
            cg_summary=cg_summary,
            balance_usdc=bal_usdc,
            balance_eth=bal_eth,
            total_value=total_val,
            peak_pct=peak_pct,
        )

        self._push_to_dashboard({
            "side": "SELL",
            "price": avg_price,
            "quantity": partial_qty,
            "reason": "partial_take_profit",
            "pnl_usdc": round(partial_pnl_usdc, 4),
            "pnl_pct": round(partial_pnl_pct, 4),
            "daily_pnl": round(self.risk._daily_pnl_usdc, 4),
            "peak_pct": round(peak_pct, 4),
        })

        logger.info(
            f"Partial TP complete: sold {partial_qty} ETH @ ${avg_price:.4f} | "
            f"PnL=${partial_pnl_usdc:.2f} (+{partial_pnl_pct:.2f}%) | "
            f"remaining={remaining_qty} ETH — trailing stop now active"
        )

    def _execute_sell(self, price: float, reason: str, cg_summary: str | None = None) -> None:
        if not self.position:
            return

        order = self.client.place_limit_sell_with_fallback(
            settings.symbol,
            self.position.quantity,
            price,
            limit_buffer_pct=settings.limit_sell_buffer_pct,
            fallback_timeout_seconds=settings.limit_sell_timeout_seconds,
        )
        avg_price = (
            float(order.get("cummulativeQuoteQty", 0)) / self.position.quantity
            if self.position.quantity else price
        )

        pnl_usdc = self.position.pnl_usdc(avg_price)
        pnl_pct = self.position.pnl_pct(avg_price)
        peak_pct = (
            (self._peak_price - self.position.entry_price) / self.position.entry_price
        ) * 100

        if reason == "stop_loss":
            self._consecutive_stop_losses += 1
            self._last_stop_loss_time = time.time()
            cd = self._cooldown_seconds()
            logger.info(
                f"Stop loss #{self._consecutive_stop_losses} — "
                f"cooldown set to {cd}s ({cd // 60} min)"
            )
        elif reason in ("trailing_stop", "take_profit"):
            self._consecutive_stop_losses = 0

        event = TradeEvent(
            symbol=settings.symbol, side="SELL",
            price=avg_price, quantity=self.position.quantity, reason=reason
        )
        self.trade_log.append(event)
        self.risk.record_trade_result(pnl_usdc)
        self._day_trades.append({"pnl_usdc": pnl_usdc, "reason": reason})

        bal_usdc, bal_eth, total_val = self._get_balances()

        self.notifier.trade_closed(
            settings.symbol, reason, pnl_usdc, pnl_pct,
            daily_pnl=self.risk._daily_pnl_usdc,
            cg_summary=cg_summary,
            balance_usdc=bal_usdc,
            balance_eth=bal_eth,
            total_value=total_val,
            peak_pct=peak_pct,
        )

        self._push_to_dashboard({
            "side": "SELL",
            "price": avg_price,
            "quantity": self.position.quantity,
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

        self._peak_price = 0.0
        self._partial_done = False
        self._active_stop_loss_pct = settings.stop_loss_pct
        self._active_take_profit_pct = settings.take_profit_pct
        self.position = None
        clear_position()

    def _push_to_dashboard(self, trade: dict) -> None:
        try:
            requests.post(DASHBOARD_URL, json=trade, timeout=2)
        except Exception:
            pass

    def _push_signal(self, signal: dict) -> None:
        """Push latest indicator snapshot to dashboard signal log."""
        try:
            requests.post(DASHBOARD_SIGNAL_URL, json=signal, timeout=1)
        except Exception:
            pass

    def _maybe_reset_daily(self) -> None:
        today = datetime.utcnow().date()
        if today != self._today:
            # Send daily summary before resetting
            self._send_daily_summary()
            logger.info(f"New trading day: {today}")
            self.risk.reset_daily()
            self._day_trades = []
            self._today = today

    def _send_daily_summary(self) -> None:
        """Compile and send the daily summary via Telegram."""
        try:
            date_str = self._today.isoformat()
            closed = [t for t in self._day_trades if t.get("pnl_usdc") is not None]
            trades_count = len(closed)
            wins = sum(1 for t in closed if t["pnl_usdc"] > 0)
            losses = sum(1 for t in closed if t["pnl_usdc"] <= 0)
            daily_pnl = sum(t["pnl_usdc"] for t in closed)
            best = max((t["pnl_usdc"] for t in closed), default=None)
            worst = min((t["pnl_usdc"] for t in closed), default=None)

            bal_usdc, _, total_val = self._get_balances()

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
