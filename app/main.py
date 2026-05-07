import sys

from loguru import logger
from prometheus_client import start_http_server

from app.services.trader import Trader

# — Logging setup ————————————————————————————————————————————————————————————
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
    start_http_server(8000)
    logger.info("Prometheus metrics server started on :8000")
    trader = Trader()
    trader.run()


if __name__ == "__main__":
    main()
