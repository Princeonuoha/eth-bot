from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass
class TradeEvent:
    symbol: str
    side: Literal["BUY", "SELL"]
    price: float
    quantity: float
    reason: str  # e.g. "signal", "take_profit", "stop_loss", "daily_limit"
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()

    @property
    def value_usdc(self) -> float:
        return self.price * self.quantity
