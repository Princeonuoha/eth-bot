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
    take_profit_pct: float = Field(default=1.0)
    stop_loss_pct: float = Field(default=0.7)
    daily_loss_limit_usdc: float = Field(default=30.0)
    rsi_oversold: float = Field(default=38.0)
    pullback_min_pct: float = Field(default=0.8)
    loop_interval_seconds: int = Field(default=30)

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
