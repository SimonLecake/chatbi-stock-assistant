"""Shared configuration and read-only SQLite access for analysis tools."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def database_path():
    path = Path(os.getenv("CHATBI_DB_PATH", "stock_prices.db"))
    return path if path.is_absolute() else ROOT / path


@contextmanager
def read_connection():
    path = database_path().resolve()
    if not path.is_file():
        raise FileNotFoundError("数据库不存在，请先运行 python prepare_data.py --demo")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.enable_load_extension(False)
    # Allow reads, SELECTs and ordinary SQL functions; deny writes, ATTACH and PRAGMA.
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
               sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK
                        if action in allowed else sqlite3.SQLITE_DENY)
    try:
        yield conn
    finally:
        conn.close()


def dataset_note():
    with read_connection() as conn:
        start, end = conn.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM stock_prices"
        ).fetchone()
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='dataset_metadata'"
        ).fetchone()
        row = conn.execute("SELECT kind FROM dataset_metadata LIMIT 1").fetchone() if table else None
    demo = bool(row and row[0] == "synthetic")
    label = "合成演示数据，不代表真实行情" if demo else "本地历史行情，非实时数据"
    return f"数据说明：{label}；数据库覆盖日期 {start} 至 {end}。"
