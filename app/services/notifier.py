"""
Notifier
────────
Sends Telegram messages on key events (trade open, close, daily halt).
Safe to use without credentials — it just logs instead.
"""

import requests
from loguru import logger

from app.config import settings


class Notifier:
    def __init__(self):
        self._enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        if self._enabled:
            logger.info("Notifier: Telegram notifications enabled")
        else:
            logger.info("Notifier: Telegram not configured — logging only")

    def send(self, message: str) -> None:
        logger.info(f"[NOTIFY] {message}")
        if not self._enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
            requests.post(
                url,
                json={"chat_id": settings.telegram_chat_id, "text": message, "parse_mode": "HTML"},
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"Notifier: Telegram send failed — {e}")

    def trade_opened(self, symbol: str, price: float, qty: float, usdc: float) -> None:
        self.send(
            f"🟢 <b>BUY</b> {symbol}\n"
            f"Price: <code>{price:.4f}</code>\n"
            f"Qty: <code>{qty:.5f} ETH</code>\n"
            f"Cost: <code>{usdc:.2f} USDC</code>"
        )

    def trade_closed(self, symbol: str, reason: str, pnl_usdc: float, pnl_pct: float) -> None:
        emoji = "💰" if pnl_usdc >= 0 else "🔴"
        self.send(
            f"{emoji} <b>SELL</b> {symbol} [{reason.upper()}]\n"
            f"PnL: <code>{pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%)</code>"
        )

    def daily_halted(self, daily_pnl: float) -> None:
        self.send(
            f"🚫 <b>Bot halted for today</b>\n"
            f"Daily PnL: <code>{daily_pnl:+.2f} USDC</code>"
        )
