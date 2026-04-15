from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Binance
    binance_api_key: str = Field(..., description="Binance API key")
    binance_api_secret: str = Field(..., description="Binance API secret")
    testnet: bool = Field(default=True, description="Use Binance Spot Testnet")

    # Strategy
    symbol: str = Field(default="ETHUSDC")
    trade_amount_usdc: float = Field(default=100.0)
    stop_loss_pct: float = Field(
        default=1.5,
        description="Minimum hard stop loss %. ATR may widen this."
    )
    take_profit_pct: float = Field(
        default=0.0,
        description=(
            "Full exit take profit %. Set to 0 to let trailing stop handle the remainder. "
            "This fires AFTER partial TP is done."
        )
    )

    # Partial take profit — hybrid exit strategy
    partial_tp_pct: float = Field(
        default=0.8,
        description=(
            "% gain at which to take partial profit. "
            "Set to 0 to disable partial TP and let trail handle everything."
        )
    )
    partial_tp_ratio: float = Field(
        default=0.5,
        description=(
            "Fraction of position to sell at partial TP. "
            "0.5 = sell 50%, keep 50% for trail."
        )
    )

    # ATR-based dynamic take profit
    atr_tp_multiplier: float = Field(
        default=1.5,
        description="ATR multiplier for dynamic TP. Higher = wider TP target in volatile markets."
    )
    atr_tp_min_pct: float = Field(
        default=0.8,
        description="Minimum dynamic TP — never exit for less than this % gain."
    )
    atr_tp_max_pct: float = Field(
        default=4.0,
        description="Maximum dynamic TP — cap the target so bot doesn't hold forever."
    )

    daily_loss_limit_usdc: float = Field(default=30.0)
    rsi_oversold: float = Field(
        default=38.0,
        description="15m RSI threshold — only buy when RSI is below this (oversold dip)."
    )
    pullback_min_pct: float = Field(
        default=0.8,
        description="Minimum % drop from recent high required before entry."
    )
    loop_interval_seconds: int = Field(default=30)

    # Trailing stop settings
    trailing_activation_pct: float = Field(
        default=1.0,
        description="How far price must rise before trailing stop activates (e.g. 1.0 = 1%)"
    )
    trailing_stop_pct: float = Field(
        default=0.8,
        description="How far price can drop from peak before exit (e.g. 0.8 = 0.8% below peak)"
    )

    # EMA slope filter
    ema_slope_min_pct: float = Field(
        default=0.0,
        description="Minimum 200 EMA slope (% over 5 candles) required to allow a buy"
    )

    # Phase 2 — Multi-timeframe RSI gate
    rsi_1h_min: float = Field(
        default=45.0,
        description=(
            "Minimum 1h RSI required to allow entry. "
            "Below this = higher timeframe is bearish, skip the trade."
        )
    )

    # Volume filter
    max_volume_ratio: float = Field(
        default=1.5,
        description="Max ratio of current volume to 20-candle average. Above this = skip buy."
    )

    # ATR-based dynamic stop loss
    atr_multiplier: float = Field(
        default=2.0,
        description="ATR multiplier for dynamic stop loss. Higher = wider stop in volatile markets."
    )

    # Slippage protection
    limit_sell_buffer_pct: float = Field(
        default=0.3,
        description="Limit sell price = trigger_price * (1 - buffer%). 0.3% gives room to fill."
    )
    limit_sell_timeout_seconds: int = Field(
        default=30,
        description="Seconds to wait for limit sell to fill before falling back to market sell."
    )

    # Phase 3 — Risk Management
    max_trades_per_day: int = Field(
        default=0,
        description=(
            "Maximum number of trades allowed per day. "
            "0 = no cap. Prevents overtrading in choppy markets."
        )
    )
    portfolio_drawdown_pct: float = Field(
        default=0.0,
        description=(
            "Halt bot if total portfolio value drops this % from its peak. "
            "0 = disabled. Example: 5.0 = halt if account drops 5% from all-time high."
        )
    )

    # Logging
    log_dir: str = Field(default="logs", description="Directory for log files")

    # Database
    db_path: str = Field(
        default="/app/data/trades.db",
        description="Path to SQLite trades database."
    )

    # Notifications
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    @property
    def base_url(self) -> str:
        return (
            "https://testnet.binance.vision"
            if self.testnet
            else "https://api.binance.com"
        )


settings = Settings()
