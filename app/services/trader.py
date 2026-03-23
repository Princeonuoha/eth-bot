"""
Trader — Main trading loop.
"""

import time
from datetime import date, datetime

import requests
from loguru import logger

from app.config import settings
from app.exchange.binance_client import BinanceClient, BotExchangeError
from app.models.position import Position
from app.models.trade_event import TradeEvent
from app.services.coingecko import CoinGeckoSentiment
from app.services.notifier import Notifier
from app.strategy.indicators import compute_ema, compute_pullback_pct, compute_rsi
from app.strategy.risk_manager import RiskManager
from app.strategy.signal_engine import is_trend_bullish, should_buy, should_sell

DASHBOARD_URL = "http://localhost:5000/api/trade"


class Trader:
    def __init__(self):
        self.client = BinanceClient()
        self.risk = RiskManager(
            daily_loss_limit_usdc=settings.daily_loss_limit_usdc,
            trade_amount_usdc=settings.trade_amount_usdc,
        )
        self.notifier = Notifier()
        self.coingecko = CoinGeckoSentiment()
        self.position: Position | None = None
        self._today: date = datetime.utcnow().date()
        self.trade_log: list[TradeEvent] = []

        # Stop loss cooldown — exponential backoff
        self._last_stop_loss_time: float = 0
        self._consecutive_stop_losses: int = 0

        # Trailing stop — tracks highest price seen since entry
        self._peak_price: float = 0.0

    def _cooldown_seconds(self) -> int:
        """15min → 30min → 60min after consecutive stop losses."""
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

        rsi = compute_rsi(closes_15m)
        ema_200 = compute_ema(closes_1h, 200)
        pullback = compute_pullback_pct(closes_15m)
        trend_ok = is_trend_bullish(price, ema_200)

        cg = self.coingecko.get_sentiment()
        cg_ok = cg is None or not cg.is_bearish()

        cooldown = self._cooldown_seconds()
        seconds_since_sl = time.time() - self._last_stop_loss_time
        in_cooldown = seconds_since_sl < cooldown

        # Update peak price while in position
        if self.position and price > self._peak_price:
            self._peak_price = price
            logger.debug(f"New peak: ${self._peak_price:.4f}")

        logger.debug(
            f"price=${price:.4f} | rsi={rsi:.1f} | pullback={pullback:.2f}% | "
            f"trend={'✅' if trend_ok else '❌'} | "
            f"cg={cg.sentiment if cg else 'unavailable'} | "
            f"peak=${self._peak_price:.4f} | "
            f"cooldown={'🕐 ' + str(int(cooldown - seconds_since_sl)) + 's' if in_cooldown else 'none'} | "
            f"position={'open' if self.position else 'none'}"
        )

        # Always check sell first — protects open position
        if self.position:
            decision = should_sell(
                entry_price=self.position.entry_price,
                current_price=price,
                peak_price=self._peak_price,
                stop_loss_pct=settings.stop_loss_pct,
                trailing_activation_pct=settings.trailing_activation_pct,
                trailing_stop_pct=settings.trailing_stop_pct,
            )
            if decision:
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
            logger.info(f"CoinGecko: skipping buy — macro is BEARISH ({cg.summary})")
            return

        if should_buy(
            trend_bullish=trend_ok,
            rsi=rsi,
            pullback_pct=pullback,
            in_position=self.position is not None,
            rsi_oversold=settings.rsi_oversold,
            pullback_min_pct=settings.pullback_min_pct,
        ):
            self._execute_buy(price, cg_summary=cg.summary if cg else None)

    def _get_balances(self) -> tuple[float, float, float]:
        """Returns (usdc, eth, total_value_usdc). Fails silently."""
        try:
            usdc = self.client.get_balance("USDC")
            eth = self.client.get_balance("ETH")
            price = self.client.get_price(settings.symbol)
            total = round(usdc + eth * price, 2)
            return round(usdc, 2), round(eth, 6), total
        except Exception as e:
            logger.warning(f"Could not fetch balances: {e}")
            return 0.0, 0.0, 0.0

    def _execute_buy(self, price: float, cg_summary: str | None = None) -> None:
        usdc_amount = self.risk.position_size_usdc()
        order = self.client.place_market_buy(settings.symbol, usdc_amount)
        qty = float(order.get("executedQty", 0))
        avg_price = float(order.get("cummulativeQuoteQty", usdc_amount)) / qty if qty else price

        self.position = Position(
            symbol=settings.symbol,
            entry_price=avg_price,
            quantity=qty,
            order_id=str(order.get("orderId", "")),
        )

        # Reset peak to entry price on new position
        self._peak_price = avg_price

        event = TradeEvent(
            symbol=settings.symbol, side="BUY", price=avg_price, quantity=qty, reason="signal"
        )
        self.trade_log.append(event)

        bal_usdc, bal_eth, total_val = self._get_balances()
        self.notifier.trade_opened(
            settings.symbol,
            avg_price,
            qty,
            usdc_amount,
            cg_summary=cg_summary,
            balance_usdc=bal_usdc,
            balance_eth=bal_eth,
            total_value=total_val,
        )
        self._push_to_dashboard(
            {
                "side": "BUY",
                "price": avg_price,
                "quantity": qty,
                "reason": "signal",
            }
        )

        logger.info(
            f"Trailing stop: activates at ${avg_price * (1 + settings.trailing_activation_pct / 100):.4f} "
            f"(+{settings.trailing_activation_pct}%) | "
            f"hard stop at ${avg_price * (1 - settings.stop_loss_pct / 100):.4f} "
            f"(-{settings.stop_loss_pct}%)"
        )

    def _execute_sell(self, price: float, reason: str, cg_summary: str | None = None) -> None:
        if not self.position:
            return

        order = self.client.place_market_sell(settings.symbol, self.position.quantity)
        avg_price = (
            float(order.get("cummulativeQuoteQty", 0)) / self.position.quantity
            if self.position.quantity
            else price
        )

        pnl_usdc = self.position.pnl_usdc(avg_price)
        pnl_pct = self.position.pnl_pct(avg_price)
        peak_pct = (
            (self._peak_price - self.position.entry_price) / self.position.entry_price
        ) * 100

        # Cooldown logic
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
            symbol=settings.symbol,
            side="SELL",
            price=avg_price,
            quantity=self.position.quantity,
            reason=reason,
        )
        self.trade_log.append(event)
        self.risk.record_trade_result(pnl_usdc)

        bal_usdc, bal_eth, total_val = self._get_balances()

        self.notifier.trade_closed(
            settings.symbol,
            reason,
            pnl_usdc,
            pnl_pct,
            daily_pnl=self.risk._daily_pnl_usdc,
            cg_summary=cg_summary,
            balance_usdc=bal_usdc,
            balance_eth=bal_eth,
            total_value=total_val,
            peak_pct=peak_pct,
        )

        self._push_to_dashboard(
            {
                "side": "SELL",
                "price": avg_price,
                "quantity": self.position.quantity,
                "reason": reason,
                "pnl_usdc": round(pnl_usdc, 4),
                "pnl_pct": round(pnl_pct, 4),
                "daily_pnl": round(self.risk._daily_pnl_usdc, 4),
                "peak_pct": round(peak_pct, 4),
            }
        )

        if self.risk.is_halted:
            self.notifier.daily_halted(
                self.risk._daily_pnl_usdc,
                balance_usdc=bal_usdc,
                total_value=total_val,
            )

        # Reset trailing state
        self._peak_price = 0.0
        self.position = None

    def _push_to_dashboard(self, trade: dict) -> None:
        """Non-blocking push to dashboard — fails silently."""
        try:
            requests.post(DASHBOARD_URL, json=trade, timeout=2)
        except Exception:
            pass

    def _maybe_reset_daily(self) -> None:
        today = datetime.utcnow().date()
        if today != self._today:
            logger.info(f"New trading day: {today}")
            self.risk.reset_daily()
            self._today = today
