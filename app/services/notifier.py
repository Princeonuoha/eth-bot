"""
Notifier
────────
Sends Telegram messages on key events.
Includes live balance and peak price in trade notifications.

Phase 4 addition:
  - daily_summary() — sent at midnight UTC with day stats
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

        trailing_activate = price * (1 + settings.trailing_activation_pct / 100)
        hard_stop = price * (1 - settings.stop_loss_pct / 100)
        partial_target = price * (1 + settings.partial_tp_pct / 100)

        lines = [
            "🟢 <b>BUY EXECUTED</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📌 Pair:      <code>{symbol}</code>",
            f"💲 Entry:     <code>${price:,.4f}</code>",
            f"📦 Qty:       <code>{qty:.5f} ETH</code>",
            f"💵 Spent:     <code>${usdc_spent:.2f} USDC</code>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"💰 Partial TP: <code>${partial_target:,.4f} (+{settings.partial_tp_pct}%) → sell {int(settings.partial_tp_ratio*100)}%</code>",
            f"🎯 Trail activates: <code>${trailing_activate:,.4f} (+{settings.trailing_activation_pct}%)</code>",
            f"🛑 Hard stop:       <code>${hard_stop:,.4f} (-{settings.stop_loss_pct}%)</code>",
            f"🕐 Time:      <code>{time_str}</code>",
        ]

        if balance_usdc is not None:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"💰 USDC left:  <code>${balance_usdc:.2f}</code>",
                f"🔷 ETH held:   <code>{balance_eth:.5f} ETH</code>"
                if balance_eth is not None else "",
                f"📊 Total val:  <code>${total_value:.2f} USDC</code>"
                if total_value is not None else "",
            ]
            lines = [line for line in lines if line]

        if cg_summary:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"📊 Market: <code>{cg_summary}</code>",
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
        peak_pct: float | None = None,
    ) -> None:
        from datetime import datetime

        time_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        emoji = "💰" if pnl_usdc >= 0 else "🔴"
        reason_label = {
            "trailing_stop": "🎯 Trailing Stop",
            "stop_loss": "🛑 Stop Loss",
            "take_profit": "✅ Take Profit",
            "partial_take_profit": "💰 Partial Take Profit",
        }.get(reason, reason.upper())

        daily_emoji = "📈" if daily_pnl >= 0 else "📉"

        lines = [
            f"{emoji} <b>SELL EXECUTED</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📌 Pair:      <code>{symbol}</code>",
            f"🏷 Reason:    <b>{reason_label}</b>",
            f"💰 Trade PnL: <code>{pnl_usdc:+.2f} USDC ({pnl_pct:+.2f}%)</code>",
        ]

        if peak_pct is not None:
            lines.append(f"🏔 Peak gain: <code>+{peak_pct:.2f}% from entry</code>")

        lines += [
            f"{daily_emoji} Day PnL:  <code>{daily_pnl:+.2f} USDC</code>",
            f"🕐 Time:      <code>{time_str}</code>",
        ]

        if balance_usdc is not None:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"💰 USDC bal:   <code>${balance_usdc:.2f}</code>",
                f"🔷 ETH held:   <code>{balance_eth:.5f} ETH</code>"
                if balance_eth is not None else "",
                f"📊 Total val:  <code>${total_value:.2f} USDC</code>"
                if total_value is not None else "",
            ]
            lines = [line for line in lines if line]

        if cg_summary:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"📊 Market: <code>{cg_summary}</code>",
            ]

        self.send("\n".join(lines))

    def daily_summary(
        self,
        date_str: str,
        daily_pnl: float,
        trades_count: int,
        wins: int,
        losses: int,
        balance_usdc: float | None = None,
        total_value: float | None = None,
        best_trade: float | None = None,
        worst_trade: float | None = None,
    ) -> None:
        """
        Sent once per day at midnight UTC.
        Summarises the day's trading performance.
        """
        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        win_rate = round((wins / trades_count * 100)) if trades_count > 0 else 0

        lines = [
            f"{pnl_emoji} <b>Daily Summary — {date_str}</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"💰 Day PnL:    <code>{daily_pnl:+.2f} USDC</code>",
            f"📊 Trades:     <code>{trades_count}</code>",
            f"✅ Wins:       <code>{wins}</code>",
            f"❌ Losses:     <code>{losses}</code>",
            f"🎯 Win rate:   <code>{win_rate}%</code>",
        ]

        if best_trade is not None:
            lines.append(f"🏆 Best trade: <code>+${best_trade:.2f} USDC</code>")
        if worst_trade is not None:
            lines.append(f"💔 Worst trade: <code>${worst_trade:.2f} USDC</code>")

        if balance_usdc is not None:
            lines += [
                "━━━━━━━━━━━━━━━━━━━━",
                f"💰 USDC bal:   <code>${balance_usdc:.2f}</code>",
            ]
        if total_value is not None:
            lines.append(f"📊 Total val:  <code>${total_value:.2f} USDC</code>")

        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("🤖 Bot continues running overnight.")

        self.send("\n".join(lines))

    def daily_halted(
        self,
        daily_pnl: float,
        balance_usdc: float | None = None,
        total_value: float | None = None,
    ) -> None:
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
            f"Hard SL:    <code>{settings.stop_loss_pct}%</code>",
            f"Trail activates: <code>+{settings.trailing_activation_pct}%</code>",
            f"Trail width:     <code>{settings.trailing_stop_pct}% from peak</code>",
            f"Partial TP: <code>{settings.partial_tp_pct}% → sell {int(settings.partial_tp_ratio*100)}%</code>",
            f"Trade size: <code>${settings.trade_amount_usdc:.0f} USDC</code>",
            f"Daily limit: <code>-${settings.daily_loss_limit_usdc:.0f} USDC</code>",
        ]
        self.send("\n".join(lines))
