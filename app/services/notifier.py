"""
Notifier
────────
Sends Telegram messages on key events.
Includes live balance in trade notifications.
"""

import requests
from loguru import logger

from app.config import settings


class Notifier:
    def __init__(self):
        self._enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        if self._enabled:
            logger.info("Notifier: Telegram notifications enabled ✅")
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
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"Notifier: Telegram send failed — {e}")

    def trade_opened(
        self,
        symbol: str,
        price: float,
        qty: float,
        usdc_spent: float,
        cg_summary: str | None = None,
        balance_usdc: float | None = None,
        balance_eth: float | None = None,
        total_value: float | None = None,
    ) -> None:
        from datetime import datetime
        time_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        lines = [
            "🟢 <b>BUY EXECUTED</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📌 Pair:    <code>{symbol}</code>",
            f"💲 Price:   <code>${price:,.4f}</code>",
            f"📦 Qty:     <code>{qty:.5f} ETH</code>",
            f"💵 Spent:   <code>${usdc_spent:.2f} USDC</code>",
            f"🕐 Time:    <code>{time_str}</code>",
        ]

        if balance_usdc is not None:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"💰 USDC left:  <code>${balance_usdc:.2f}</code>",
                f"🔷 ETH held:   <code>{balance_eth:.5f} ETH</code>" if balance_eth is not None else "",
                f"📊 Total val:  <code>${total_value:.2f} USDC</code>" if total_value is not None else "",
            ]
            lines = [l for line in lines if line]  # remove empty

        if cg_summary:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"📊 Market:  <code>{cg_summary}</code>",
            ]

        self.send("\n".join(lines))

    def trade_closed(
        self,
        symbol: str,
        reason: str,
        pnl_usdc: float,
        pnl_pct: float,
        daily_pnl: float = 0.0,
        cg_summary: str | None = None,
        balance_usdc: float | None = None,
        balance_eth: float | None = None,
        total_value: float | None = None,
    ) -> None:
        from datetime import datetime
        time_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        emoji = "💰" if pnl_usdc >= 0 else "🔴"
        reason_label = {
            "take_profit": "✅ Take Profit",
            "stop_loss": "🛑 Stop Loss",
        }.get(reason, reason.upper())

        daily_emoji = "📈" if daily_pnl >= 0 else "📉"

        lines = [
            f"{emoji} <b>SELL EXECUTED</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📌 Pair:      <code>{symbol}</code>",
            f"🏷 Reason:    <b>{reason_label}</b>",
            f"💰 Trade PnL: <code>{pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%)</code>",
            f"{daily_emoji} Day PnL:  <code>{daily_pnl:+.2f} USDC</code>",
            f"🕐 Time:      <code>{time_str}</code>",
        ]

        if balance_usdc is not None:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"💰 USDC bal:   <code>${balance_usdc:.2f}</code>",
                f"🔷 ETH held:   <code>{balance_eth:.5f} ETH</code>" if balance_eth is not None else "",
                f"📊 Total val:  <code>${total_value:.2f} USDC</code>" if total_value is not None else "",
            ]
            lines = [l for line in lines if line]

        if cg_summary:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"📊 Market:    <code>{cg_summary}</code>",
            ]

        self.send("\n".join(lines))

    def daily_halted(self, daily_pnl: float, balance_usdc: float | None = None, total_value: float | None = None) -> None:
        from datetime import datetime
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

        lines = [
            "🚫 <b>BOT HALTED FOR TODAY</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📅 Date:      <code>{date_str}</code>",
            f"📉 Day PnL:   <code>{daily_pnl:+.2f} USDC</code>",
        ]
        if balance_usdc is not None:
            lines.append(f"💰 USDC bal:  <code>${balance_usdc:.2f}</code>")
        if total_value is not None:
            lines.append(f"📊 Total val: <code>${total_value:.2f} USDC</code>")
        lines.append("ℹ️ Bot will resume tomorrow at midnight UTC.")
        self.send("\n".join(lines))

    def bot_started(self, testnet: bool) -> None:
        mode = "🧪 TESTNET" if testnet else "🔴 LIVE"
        lines = [
            "🤖 <b>ETH Bot Started</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"Mode: <b>{mode}</b>",
            "Pair: <code>ETHUSDC</code>",
            f"TP: <code>{settings.take_profit_pct}%</code> | SL: <code>{settings.stop_loss_pct}%</code>",
            f"Trade size: <code>${settings.trade_amount_usdc:.0f} USDC</code>",
            f"Daily limit: <code>-${settings.daily_loss_limit_usdc:.0f} USDC</code>",
        ]
        self.send("\n".join(lines))
