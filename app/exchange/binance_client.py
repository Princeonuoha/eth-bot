"""
Binance Client Wrapper
──────────────────────
Thin wrapper around the official binance-connector SDK.
Handles testnet vs live routing automatically from config.
All raw API errors are caught here and re-raised as BotExchangeError.
"""

import time

import pandas as pd
from binance.spot import Spot
from loguru import logger

from app.config import settings


class BotExchangeError(Exception):
    pass


class BinanceClient:
    def __init__(self):
        self._client = Spot(
            api_key=settings.binance_api_key,
            api_secret=settings.binance_api_secret,
            base_url=settings.base_url,
        )
        mode = "TESTNET" if settings.testnet else "LIVE"
        logger.info(f"BinanceClient: connected ({mode}) — {settings.base_url}")

    # ── Market Data ────────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float:
        """Latest mid price from the order book ticker."""
        try:
            data = self._client.book_ticker(symbol)
            bid = float(data["bidPrice"])
            ask = float(data["askPrice"])
            return (bid + ask) / 2
        except Exception as e:
            raise BotExchangeError(f"get_price failed: {e}") from e

    def get_candles(self, symbol: str, interval: str, limit: int = 250) -> pd.DataFrame:
        """
        Returns a DataFrame with columns: open_time, open, high, low, close, volume.
        interval examples: '15m', '1h', '4h'
        """
        try:
            raw = self._client.klines(symbol, interval, limit=limit)
            df = pd.DataFrame(
                raw,
                columns=[
                    "open_time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "close_time",
                    "quote_volume",
                    "trades",
                    "taker_base",
                    "taker_quote",
                    "ignore",
                ],
            )
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = df[col].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            return df[["open_time", "open", "high", "low", "close", "volume"]]
        except Exception as e:
            raise BotExchangeError(f"get_candles failed: {e}") from e

    # ── Account ────────────────────────────────────────────────────────────────

    def get_balance(self, asset: str) -> float:
        """Free balance for a given asset, e.g. 'USDC' or 'ETH'."""
        try:
            account = self._client.account()
            for b in account["balances"]:
                if b["asset"] == asset:
                    return float(b["free"])
            return 0.0
        except Exception as e:
            raise BotExchangeError(f"get_balance failed: {e}") from e

    # ── Orders ─────────────────────────────────────────────────────────────────

    def place_market_buy(self, symbol: str, quote_amount: float) -> dict:
        """
        Buy using a fixed USDC amount (quoteOrderQty).
        Returns the full order response dict.
        """
        try:
            logger.info(f"ORDER: MARKET BUY {symbol} quoteQty={quote_amount:.2f} USDC")
            order = self._client.new_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quoteOrderQty=quote_amount,
            )
            logger.info(f"ORDER FILLED: {order}")
            return order
        except Exception as e:
            raise BotExchangeError(f"place_market_buy failed: {e}") from e

    def place_market_sell(self, symbol: str, quantity: float) -> dict:
        """
        Sell a specific ETH quantity at market price.
        Always fills but may suffer slippage in volatile conditions.
        Prefer place_limit_sell_with_fallback for stop loss exits.
        """
        try:
            logger.info(f"ORDER: MARKET SELL {symbol} qty={quantity:.6f} ETH")
            order = self._client.new_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=round(quantity, 5),
            )
            logger.info(f"ORDER FILLED: {order}")
            return order
        except Exception as e:
            raise BotExchangeError(f"place_market_sell failed: {e}") from e

    def place_limit_sell_with_fallback(
        self,
        symbol: str,
        quantity: float,
        trigger_price: float,
        limit_buffer_pct: float = 0.3,
        fallback_timeout_seconds: int = 30,
    ) -> dict:
        """
        Place a limit sell slightly below trigger price to avoid slippage.
        If the limit order doesn't fill within fallback_timeout_seconds, cancel
        it and fall back to a market sell.

        Why this matters:
          A market sell during a flash crash fills at whatever exists — could be
          5-10% below your stop price. A limit sell guarantees you don't sell
          worse than your chosen price, at the cost of possibly not filling.
          The fallback ensures you always exit — you just get a small window
          to try for the better price first.

        Args:
            trigger_price: The price that triggered the sell decision.
            limit_buffer_pct: How far below trigger to place the limit (default 0.3%).
                              0.3% gives room to fill without much extra loss.
            fallback_timeout_seconds: Seconds to wait for limit fill before going market.
        """
        limit_price = round(trigger_price * (1 - limit_buffer_pct / 100), 2)
        qty_rounded = round(quantity, 5)

        logger.info(
            f"ORDER: LIMIT SELL {symbol} qty={qty_rounded} "
            f"limit=${limit_price:.2f} (trigger=${trigger_price:.2f}, "
            f"buffer={limit_buffer_pct}%)"
        )

        try:
            order = self._client.new_order(
                symbol=symbol,
                side="SELL",
                type="LIMIT",
                timeInForce="GTC",
                quantity=qty_rounded,
                price=str(limit_price),
            )
            order_id = order["orderId"]
            logger.info(f"LIMIT ORDER PLACED: id={order_id} @ ${limit_price:.2f}")
        except Exception as e:
            raise BotExchangeError(f"place_limit_sell failed: {e}") from e

        # Poll for fill — check every 3 seconds
        deadline = time.time() + fallback_timeout_seconds
        poll_interval = 3

        while time.time() < deadline:
            time.sleep(poll_interval)
            try:
                status = self._client.get_order(symbol=symbol, orderId=order_id)
                order_status = status["status"]

                if order_status == "FILLED":
                    logger.info(f"LIMIT SELL FILLED: id={order_id} @ ${limit_price:.2f} ✅")
                    return status

                if order_status in ("CANCELED", "REJECTED", "EXPIRED"):
                    logger.warning(
                        f"Limit order {order_id} is {order_status} — falling back to market sell"
                    )
                    break

                logger.debug(
                    f"Limit order {order_id} status={order_status} — "
                    f"waiting ({int(deadline - time.time())}s left)"
                )
            except Exception as e:
                logger.warning(f"Error polling order {order_id}: {e} — will retry")

        # Timeout reached or order failed — cancel limit and go market
        logger.warning(
            f"Limit sell timeout ({fallback_timeout_seconds}s) — "
            f"cancelling {order_id} and placing market sell"
        )
        try:
            self._client.cancel_order(symbol=symbol, orderId=order_id)
            logger.info(f"Limit order {order_id} cancelled")
        except Exception as e:
            logger.warning(
                f"Could not cancel limit order {order_id}: {e} — proceeding to market sell"
            )

        return self.place_market_sell(symbol, quantity)

    def get_symbol_info(self, symbol: str) -> dict:
        """Raw exchange info for a symbol (step sizes, min qty, etc.)."""
        try:
            info = self._client.exchange_info(symbol=symbol)
            return info["symbols"][0]
        except Exception as e:
            raise BotExchangeError(f"get_symbol_info failed: {e}") from e
