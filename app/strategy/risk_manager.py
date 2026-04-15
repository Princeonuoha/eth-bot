"""
Risk Manager
────────────
Guards daily loss limits, enforces position sizing, and halts
trading when drawdown thresholds are breached.

Phase 3 additions:
  - Volatility-scaled position sizing — reduce size when ATR is high
  - Max trades per day cap — prevents overtrading
  - Portfolio drawdown halting — halts if account drops X% from all-time high
"""

from loguru import logger


class RiskManager:
    def __init__(
        self,
        daily_loss_limit_usdc: float,
        trade_amount_usdc: float,
        max_trades_per_day: int = 0,
        portfolio_drawdown_pct: float = 0.0,
        initial_portfolio_value: float = 0.0,
    ):
        self.daily_loss_limit_usdc = daily_loss_limit_usdc
        self.trade_amount_usdc = trade_amount_usdc
        self.max_trades_per_day = max_trades_per_day          # 0 = no cap
        self.portfolio_drawdown_pct = portfolio_drawdown_pct  # 0 = disabled
        self._daily_pnl_usdc: float = 0.0
        self._trades_today: int = 0
        self._halted: bool = False

        # Portfolio drawdown tracking
        # Peak portfolio value seen since bot started — used to measure drawdown
        self._peak_portfolio_value: float = initial_portfolio_value
        self._portfolio_halted: bool = False

    @property
    def is_halted(self) -> bool:
        return self._halted or self._portfolio_halted

    def record_trade_result(self, pnl_usdc: float) -> None:
        """Call this after every closed trade."""
        self._daily_pnl_usdc += pnl_usdc
        self._trades_today += 1
        logger.info(
            f"RiskManager: trade closed | pnl={pnl_usdc:+.2f} USDC | "
            f"daily_pnl={self._daily_pnl_usdc:+.2f} USDC | "
            f"trades={self._trades_today}"
            + (f"/{self.max_trades_per_day}" if self.max_trades_per_day > 0 else "")
        )
        if self._daily_pnl_usdc <= -self.daily_loss_limit_usdc:
            logger.warning(
                f"RiskManager: daily loss limit hit ({self._daily_pnl_usdc:.2f} USDC). "
                "Halting for the day. 🚫"
            )
            self._halted = True

    def update_portfolio_value(self, current_value: float) -> None:
        """
        Call this each tick with the current total portfolio value (USDC + ETH * price).
        Updates the peak and checks if drawdown threshold has been breached.

        Example: portfolio_drawdown_pct=5.0 means halt if account drops 5%
        from its all-time high since the bot started.
        """
        if self.portfolio_drawdown_pct <= 0:
            return  # disabled

        # Update peak
        if current_value > self._peak_portfolio_value:
            self._peak_portfolio_value = current_value
            logger.debug(f"RiskManager: new portfolio peak ${current_value:.2f}")

        # Check drawdown
        if self._peak_portfolio_value > 0:
            drawdown_pct = (
                (self._peak_portfolio_value - current_value) / self._peak_portfolio_value
            ) * 100

            if drawdown_pct >= self.portfolio_drawdown_pct and not self._portfolio_halted:
                logger.warning(
                    f"RiskManager: portfolio drawdown {drawdown_pct:.2f}% exceeds limit "
                    f"{self.portfolio_drawdown_pct}% | "
                    f"peak=${self._peak_portfolio_value:.2f} | "
                    f"current=${current_value:.2f} | "
                    f"Halting bot. 🚫"
                )
                self._portfolio_halted = True

    def can_trade(self) -> bool:
        """Returns True if the bot is allowed to open a new position."""
        if self._halted:
            logger.warning("RiskManager: daily loss limit reached — halted for today")
            return False

        if self._portfolio_halted:
            logger.warning("RiskManager: portfolio drawdown limit reached — halted")
            return False

        if self.max_trades_per_day > 0 and self._trades_today >= self.max_trades_per_day:
            logger.info(
                f"RiskManager: max trades per day reached "
                f"({self._trades_today}/{self.max_trades_per_day}) — skipping"
            )
            return False

        return True

    def position_size_usdc(self, atr_pct: float = 0.0) -> float:
        """
        Returns the USDC amount to use for the next trade.

        Volatility-scaled sizing:
          When ATR is high (volatile market), reduce position size to limit risk.
          When ATR is normal or low, use full size.

          Scaling logic:
            atr_pct < 1.5% → full size (normal volatility)
            atr_pct 1.5–2.5% → scale down to 75% (elevated volatility)
            atr_pct 2.5–4.0% → scale down to 50% (high volatility)
            atr_pct > 4.0% → scale down to 25% (extreme volatility)

          This means in a calm market you trade $1000, in a wild market $250.
          Risk per trade stays roughly constant in dollar terms.

        Args:
            atr_pct: Current ATR as % of price. Pass 0 to use full size.
        """
        size = self.trade_amount_usdc

        if atr_pct > 0:
            if atr_pct > 4.0:
                scale = 0.25
            elif atr_pct > 2.5:
                scale = 0.50
            elif atr_pct > 1.5:
                scale = 0.75
            else:
                scale = 1.0

            scaled_size = round(size * scale, 2)

            if scale < 1.0:
                logger.info(
                    f"RiskManager: volatility scaling | ATR={atr_pct:.2f}% | "
                    f"scale={int(scale * 100)}% | "
                    f"size=${size:.0f} → ${scaled_size:.0f}"
                )
            return scaled_size

        return size

    def reset_daily(self) -> None:
        """Call this at the start of each trading day."""
        logger.info(
            f"RiskManager: resetting daily stats | "
            f"yesterday pnl={self._daily_pnl_usdc:+.2f} USDC | "
            f"trades={self._trades_today}"
        )
        self._daily_pnl_usdc = 0.0
        self._trades_today = 0
        self._halted = False
        # Note: portfolio_halted is NOT reset daily — it's a hard stop
        # until manually cleared or bot restarted
