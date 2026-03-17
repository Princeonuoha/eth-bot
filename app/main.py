import sys
from loguru import logger
from app.services.trader import Trader

# ── Logging setup ─────────────────────────────────────────────────────────────
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="DEBUG",
)
logger.add(
    "logs/bot.log",
    rotation="1 day",
    retention="14 days",
    compression="zip",
    level="INFO",
)


def main():
    trader = Trader()
    trader.run()


if __name__ == "__main__":
    main()
