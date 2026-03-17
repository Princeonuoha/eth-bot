"""
Binance Client Wrapper
──────────────────────
Thin wrapper around the official binance-connector SDK.
Handles testnet vs live routing automatically from config.
All raw API errors are caught here and re-raised as BotExchangeError.
"""

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
                    "open_time", "open", "high", "low", "close", "volume",
                    "close_time", "quote_volume", "trades",
                    "taker_base", "taker_quote", "ignore",
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
        Sell a specific ETH quantity.
        Returns the full order response dict.
        """
        try:
            logger.info(f"ORDER: MARKET SELL {symbol} qty={quantity:.6f} ETH")
            order = self._client.new_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=round(quantity, 5),  # Binance step-size safe rounding
            )
            logger.info(f"ORDER FILLED: {order}")
            return order
        except Exception as e:
            raise BotExchangeError(f"place_market_sell failed: {e}") from e

    def get_symbol_info(self, symbol: str) -> dict:
        """Raw exchange info for a symbol (step sizes, min qty, etc.)."""
        try:
            info = self._client.exchange_info(symbol=symbol)
            return info["symbols"][0]
        except Exception as e:
            raise BotExchangeError(f"get_symbol_info failed: {e}") from e
