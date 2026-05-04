"""
Position Store
──────────────
Persists open positions to SQLite so bot survives restarts without
orphaning positions or double-buying.

Supports multiple symbols — each symbol gets its own row keyed by symbol.

On entry  → save_position(symbol, ...)
On exit   → clear_position(symbol)
On startup → load_position(symbol)

Schema note: sl_order_id stores the Binance STOP_LOSS_LIMIT order ID
placed at entry. Used to poll fill status and cancel/replace on trail updates.
"""

import os
import sqlite3
from datetime import datetime
from threading import Lock

from loguru import logger

from app.models.position import Position

DB_PATH = os.environ.get("DB_PATH", "trades.db")
_lock = Lock()


def _get_conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS open_position (
            symbol          TEXT PRIMARY KEY,
            entry_price     REAL,
            quantity        REAL,
            order_id        TEXT,
            entry_time      TEXT,
            peak_price      REAL,
            stop_loss_pct   REAL,
            take_profit_pct REAL,
            sl_order_id     TEXT,
            saved_at        TEXT
        )
    """)
    # Migration: add sl_order_id to existing tables that predate this column
    try:
        con.execute("ALTER TABLE open_position ADD COLUMN sl_order_id TEXT")
        con.commit()
        logger.info("DB migration: added sl_order_id column to open_position")
    except sqlite3.OperationalError:
        pass  # Column already exists — normal case
    con.commit()
    return con


def save_position(
    position: Position,
    peak_price: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    sl_order_id: str | None = None,
) -> None:
    """Upsert the current open position for a symbol into SQLite."""
    with _lock:
        con = _get_conn()
        con.execute("""
            INSERT INTO open_position
                (symbol, entry_price, quantity, order_id, entry_time,
                 peak_price, stop_loss_pct, take_profit_pct, sl_order_id, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                entry_price     = excluded.entry_price,
                quantity        = excluded.quantity,
                order_id        = excluded.order_id,
                entry_time      = excluded.entry_time,
                peak_price      = excluded.peak_price,
                stop_loss_pct   = excluded.stop_loss_pct,
                take_profit_pct = excluded.take_profit_pct,
                sl_order_id     = excluded.sl_order_id,
                saved_at        = excluded.saved_at
        """, (
            position.symbol,
            position.entry_price,
            position.quantity,
            position.order_id or "",
            position.entry_time.isoformat(),
            peak_price,
            stop_loss_pct,
            take_profit_pct,
            sl_order_id or "",
            datetime.utcnow().isoformat(),
        ))
        con.commit()
        con.close()
    logger.debug(
        f"[{position.symbol}] Position saved: entry={position.entry_price} "
        f"qty={position.quantity} peak={peak_price} sl_order_id={sl_order_id}"
    )


def update_peak(symbol: str, peak_price: float) -> None:
    """Update just the peak price for a symbol — called every tick while in position."""
    with _lock:
        con = _get_conn()
        con.execute(
            "UPDATE open_position SET peak_price = ?, saved_at = ? WHERE symbol = ?",
            (peak_price, datetime.utcnow().isoformat(), symbol),
        )
        con.commit()
        con.close()


def update_sl_order_id(symbol: str, sl_order_id: str) -> None:
    """
    Update the exchange SL order ID for a symbol.
    Called after cancelling + replacing a stop loss order (trail updates).
    """
    with _lock:
        con = _get_conn()
        con.execute(
            "UPDATE open_position SET sl_order_id = ?, saved_at = ? WHERE symbol = ?",
            (sl_order_id, datetime.utcnow().isoformat(), symbol),
        )
        con.commit()
        con.close()
    logger.debug(f"[{symbol}] sl_order_id updated → {sl_order_id}")


def clear_position(symbol: str) -> None:
    """Remove the open position for a symbol — called after every successful sell."""
    with _lock:
        con = _get_conn()
        con.execute("DELETE FROM open_position WHERE symbol = ?", (symbol,))
        con.commit()
        con.close()
    logger.info(f"[{symbol}] Position cleared from DB")


def load_position(symbol: str) -> tuple[Position | None, float, float, float, str | None]:
    """
    Load open position for a symbol from DB on startup.

    Returns (position, peak_price, stop_loss_pct, take_profit_pct, sl_order_id)
    or (None, 0.0, 0.0, 0.0, None) if no position exists.

    sl_order_id is the Binance STOP_LOSS_LIMIT order ID — on restart we
    re-verify its status to detect any fills that happened while bot was down.
    """
    with _lock:
        con = _get_conn()
        row = con.execute("""
            SELECT symbol, entry_price, quantity, order_id,
                   entry_time, peak_price, stop_loss_pct, take_profit_pct, sl_order_id
            FROM open_position WHERE symbol = ?
        """, (symbol,)).fetchone()
        con.close()

    if not row:
        return None, 0.0, 0.0, 0.0, None

    sym, entry_price, quantity, order_id, entry_time, peak_price, sl_pct, tp_pct, sl_order_id = row

    try:
        entry_dt = datetime.fromisoformat(entry_time)
    except Exception:
        entry_dt = datetime.utcnow()

    position = Position(
        symbol=sym,
        entry_price=entry_price,
        quantity=quantity,
        order_id=order_id,
        entry_time=entry_dt,
    )

    logger.warning(
        f"[{symbol}] ⚠️  Restoring open position from DB: "
        f"entry=${entry_price} qty={quantity} peak=${peak_price} "
        f"SL={sl_pct}% TP={tp_pct}% sl_order_id={sl_order_id}"
    )
    return position, peak_price, sl_pct, tp_pct, sl_order_id or None
