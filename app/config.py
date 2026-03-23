from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        default=1.5, description="Minimum hard stop loss %. ATR may widen this."
    )
    daily_loss_limit_usdc: float = Field(default=30.0)
    rsi_oversold: float = Field(default=38.0)
    pullback_min_pct: float = Field(default=0.8)
    loop_interval_seconds: int = Field(default=30)

    # Trailing stop settings
    trailing_activation_pct: float = Field(
        default=1.0,
        description="How far price must rise before trailing stop activates (e.g. 1.0 = 1%)",
    )
    trailing_stop_pct: float = Field(
        default=0.8,
        description="How far price can drop from peak before exit (e.g. 0.8 = 0.8% below peak)",
    )

    # EMA slope filter
    # Prevents buying when price briefly crosses 200 EMA during a downtrend.
    # 0.0 = any positive slope passes. 0.02–0.05 = stricter (EMA must be visibly rising).
    ema_slope_min_pct: float = Field(
        default=0.0, description="Minimum 200 EMA slope (% over 5 candles) required to allow a buy"
    )

    # Volume filter
    # Blocks entry if current candle volume is a large spike vs recent average.
    # Spikes on down candles = panic selling — not a good time to buy.
    max_volume_ratio: float = Field(
        default=1.5,
        description="Max ratio of current volume to 20-candle average. Above this = skip buy.",
    )

    # ATR-based dynamic stop loss
    # Widens stop loss in volatile markets to avoid being shaken out by noise.
    # Final stop = max(stop_loss_pct, ATR * atr_multiplier).
    atr_multiplier: float = Field(
        default=2.0,
        description="ATR multiplier for dynamic stop loss. Higher = wider stop in volatile markets.",
    )

    # Slippage protection — limit sell with market fallback
    # On stop loss / trailing stop exits, tries a limit sell first to avoid slippage.
    # If unfilled within timeout, cancels and goes market.
    limit_sell_buffer_pct: float = Field(
        default=0.3,
        description="Limit sell price = trigger_price * (1 - buffer%). 0.3% gives room to fill.",
    )
    limit_sell_timeout_seconds: int = Field(
        default=30,
        description="Seconds to wait for limit sell to fill before falling back to market sell.",
    )

    # Notifications
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    @property
    def base_url(self) -> str:
        return "https://testnet.binance.vision" if self.testnet else "https://api.binance.com"


settings = Settings()
