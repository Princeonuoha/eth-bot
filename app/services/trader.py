"""
Trader
──────
Main trading loop. Ties together:
  - BinanceClient  (exchange data + order execution)
  - SignalEngine   (buy/sell decisions)
  - RiskManager    (daily loss limit, position sizing)
  - Notifier       (alerts)

Run via: python -m app.main
"""

import time
from datetime import datetime, date

from loguru import logger

from app.config import settings
from app.exchange.binance_client import BinanceClient, BotExchangeError
from app.models.position import Position
from app.models.trade_event import TradeEvent
from app.services.notifier import Notifier
from app.strategy.indicators import (
    compute_rsi,
    compute_ema,
    compute_pullback_pct,
)
from app.strategy.risk_manager import RiskManager
from app.strategy.signal_engine import is_trend_bullish, should_buy, should_sell


class Trader:
    def __init__(self):
        self.client = BinanceClient()
        self.risk = RiskManager(
            daily_loss_limit_usdc=settings.daily_loss_limit_usdc,
            trade_amount_usdc=settings.trade_amount_usdc,
        )
        self.notifier = Notifier()
        self.position: Position | None = None
        self._today: date = datetime.utcnow().date()
        self.trade_log: list[TradeEvent] = []

    # ── Main Loop ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info("=" * 60)
        logger.info(f"  ETH Bot starting | symbol={settings.symbol}")
        logger.info(f"  Testnet={settings.testnet} | TP={settings.take_profit_pct}% | SL={settings.stop_loss_pct}%")
        logger.info("=" * 60)

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

    # ── Core Tick ──────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        symbol = settings.symbol

        # 1. Fetch market data
        price = self.client.get_price(symbol)
        candles_15m = self.client.get_candles(symbol, "15m", limit=250)
        candles_1h = self.client.get_candles(symbol, "1h", limit=250)

        closes_15m = candles_15m["close"]
        closes_1h = candles_1h["close"]

        rsi = compute_rsi(closes_15m)
        ema_200 = compute_ema(closes_1h, 200)
        pullback = compute_pullback_pct(closes_15m)
        trend_ok = is_trend_bullish(price, ema_200)

        logger.debug(
            f"price={price:.4f} | ema200={ema_200:.4f} | "
            f"rsi={rsi:.1f} | pullback={pullback:.2f}% | "
            f"trend={'✅' if trend_ok else '❌'} | "
            f"position={'open' if self.position else 'none'}"
        )

        # 2. Check sell first (protects open position regardless of halt)
        if self.position:
            decision = should_sell(
                entry_price=self.position.entry_price,
                current_price=price,
                take_profit_pct=settings.take_profit_pct,
                stop_loss_pct=settings.stop_loss_pct,
            )
            if decision:
                self._execute_sell(price, decision)
                return

        # 3. Check buy
        if not self.risk.can_trade():
            return

        if should_buy(
            trend_bullish=trend_ok,
            rsi=rsi,
            pullback_pct=pullback,
            in_position=self.position is not None,
            rsi_oversold=settings.rsi_oversold,
            pullback_min_pct=settings.pullback_min_pct,
        ):
            self._execute_buy(price)

    # ── Order Execution ────────────────────────────────────────────────────────

    def _execute_buy(self, price: float) -> None:
        usdc_amount = self.risk.position_size_usdc()
        order = self.client.place_market_buy(settings.symbol, usdc_amount)

        # Parse filled quantity from order response
        qty = float(order.get("executedQty", 0))
        avg_price = float(order.get("cummulativeQuoteQty", usdc_amount)) / qty if qty else price

        self.position = Position(
            symbol=settings.symbol,
            entry_price=avg_price,
            quantity=qty,
            order_id=str(order.get("orderId", "")),
        )

        event = TradeEvent(
            symbol=settings.symbol,
            side="BUY",
            price=avg_price,
            quantity=qty,
            reason="signal",
        )
        self.trade_log.append(event)
        self.notifier.trade_opened(settings.symbol, avg_price, qty, usdc_amount)

    def _execute_sell(self, price: float, reason: str) -> None:
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

        event = TradeEvent(
            symbol=settings.symbol,
            side="SELL",
            price=avg_price,
            quantity=self.position.quantity,
            reason=reason,
        )
        self.trade_log.append(event)
        self.risk.record_trade_result(pnl_usdc)
        self.notifier.trade_closed(settings.symbol, reason, pnl_usdc, pnl_pct)

        if self.risk.is_halted:
            self.notifier.daily_halted(pnl_usdc)

        self.position = None

    # ── Daily Reset ────────────────────────────────────────────────────────────

    def _maybe_reset_daily(self) -> None:
        today = datetime.utcnow().date()
        if today != self._today:
            logger.info(f"New trading day: {today}")
            self.risk.reset_daily()
            self._today = today
