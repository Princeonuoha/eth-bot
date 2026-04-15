"""
Position Store
──────────────
Persists open position to SQLite so bot survives restarts without
orphaning positions or double-buying.

On entry  → save position to DB
On exit   → clear position from DB
On startup → reload position from DB if one exists
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
            id          INTEGER PRIMARY KEY CHECK (id = 1),  -- only one row ever
            symbol      TEXT,
            entry_price REAL,
            quantity    REAL,
            order_id    TEXT,
            entry_time  TEXT,
            peak_price  REAL,
            stop_loss_pct  REAL,
            take_profit_pct REAL,
            saved_at    TEXT
        )
    """)
    con.commit()
    return con


def save_position(
    position: Position,
    peak_price: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> None:
    """
    Upsert the current open position into SQLite.
    Called after every successful buy order.
    """
    with _lock:
        con = _get_conn()
        con.execute("""
            INSERT INTO open_position
                (id, symbol, entry_price, quantity, order_id, entry_time,
                 peak_price, stop_loss_pct, take_profit_pct, saved_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                symbol          = excluded.symbol,
                entry_price     = excluded.entry_price,
                quantity        = excluded.quantity,
                order_id        = excluded.order_id,
                entry_time      = excluded.entry_time,
                peak_price      = excluded.peak_price,
                stop_loss_pct   = excluded.stop_loss_pct,
                take_profit_pct = excluded.take_profit_pct,
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
            datetime.utcnow().isoformat(),
        ))
        con.commit()
        con.close()
    logger.debug(
        f"Position saved to DB: entry={position.entry_price} "
        f"qty={position.quantity} peak={peak_price}"
    )


def update_peak(peak_price: float) -> None:
    """Update just the peak price — called every tick while in position."""
    with _lock:
        con = _get_conn()
        con.execute(
            "UPDATE open_position SET peak_price = ?, saved_at = ? WHERE id = 1",
            (peak_price, datetime.utcnow().isoformat()),
        )
        con.commit()
        con.close()


def clear_position() -> None:
    """Remove the open position — called after every successful sell."""
    with _lock:
        con = _get_conn()
        con.execute("DELETE FROM open_position WHERE id = 1")
        con.commit()
        con.close()
    logger.info("Position cleared from DB")


def load_position() -> tuple[Position | None, float, float, float]:
    """
    Load open position from DB on startup.
    Returns (position, peak_price, stop_loss_pct, take_profit_pct)
    or (None, 0.0, 0.0, 0.0) if no position exists.
    """
    with _lock:
        con = _get_conn()
        row = con.execute("""
            SELECT symbol, entry_price, quantity, order_id,
                   entry_time, peak_price, stop_loss_pct, take_profit_pct
            FROM open_position WHERE id = 1
        """).fetchone()
        con.close()

    if not row:
        return None, 0.0, 0.0, 0.0

    symbol, entry_price, quantity, order_id, entry_time, peak_price, sl_pct, tp_pct = row

    try:
        entry_dt = datetime.fromisoformat(entry_time)
    except Exception:
        entry_dt = datetime.utcnow()

    position = Position(
        symbol=symbol,
        entry_price=entry_price,
        quantity=quantity,
        order_id=order_id,
        entry_time=entry_dt,
    )

    logger.warning(
        f"⚠️  Restoring open position from DB: "
        f"entry=${entry_price} qty={quantity} peak=${peak_price} "
        f"SL={sl_pct}% TP={tp_pct}%"
    )
    return position, peak_price, sl_pct, tp_pct
