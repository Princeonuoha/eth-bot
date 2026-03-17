from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Position:
    symbol: str
    entry_price: float
    quantity: float
    entry_time: datetime = field(default_factory=datetime.utcnow)
    order_id: Optional[str] = None

    @property
    def cost_usdc(self) -> float:
        return self.entry_price * self.quantity

    def pnl_pct(self, current_price: float) -> float:
        return ((current_price - self.entry_price) / self.entry_price) * 100

    def pnl_usdc(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.quantity
