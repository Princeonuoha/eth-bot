"""
Risk Manager
────────────
Guards daily loss limits and enforces position sizing rules.
The trader calls this before placing any order.
"""

from loguru import logger


class RiskManager:
    def __init__(self, daily_loss_limit_usdc: float, trade_amount_usdc: float):
        self.daily_loss_limit_usdc = daily_loss_limit_usdc
        self.trade_amount_usdc = trade_amount_usdc
        self._daily_pnl_usdc: float = 0.0
        self._trades_today: int = 0
        self._halted: bool = False

    @property
    def is_halted(self) -> bool:
        return self._halted

    def record_trade_result(self, pnl_usdc: float) -> None:
        """Call this after every closed trade."""
        self._daily_pnl_usdc += pnl_usdc
        self._trades_today += 1
        logger.info(
            f"RiskManager: trade closed | pnl={pnl_usdc:+.2f} USDC | "
            f"daily_pnl={self._daily_pnl_usdc:+.2f} USDC | trades={self._trades_today}"
        )

        if self._daily_pnl_usdc <= -self.daily_loss_limit_usdc:
            logger.warning(
                f"RiskManager: daily loss limit hit ({self._daily_pnl_usdc:.2f} USDC). "
                "Halting for the day. 🚫"
            )
            self._halted = True

    def can_trade(self) -> bool:
        if self._halted:
            logger.warning("RiskManager: bot is halted for today — skipping signal")
            return False
        return True

    def position_size_usdc(self) -> float:
        return self.trade_amount_usdc

    def reset_daily(self) -> None:
        """Call this at the start of each trading day."""
        logger.info(
            f"RiskManager: resetting daily stats | "
            f"yesterday pnl={self._daily_pnl_usdc:+.2f} USDC"
        )
        self._daily_pnl_usdc = 0.0
        self._trades_today = 0
        self._halted = False
