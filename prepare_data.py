"""Create a reproducible demo database or import Tushare daily market data."""
import argparse
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from db import database_path

STOCKS = {
    "600519.SH": "贵州茅台", "000858.SZ": "五粮液", "000776.SZ": "广发证券",
    "688981.SH": "中芯国际", "002415.SZ": "海康威视", "600570.SH": "恒生电子",
    "601916.SH": "浙商银行",
}
COLUMNS = ["ts_code", "stock_name", "trade_date", "open", "high", "low", "close",
           "pre_close", "change", "pct_chg", "vol", "amount"]


def demo_frame():
    """Fixed seed and dates: reproducible synthetic prices, not exchange data."""
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("20250101", "20251231")
    frames = []
    for index, (code, name) in enumerate(STOCKS.items()):
        close = (30 + index * 20) * np.exp(np.cumsum(rng.normal(0.0004, 0.015, len(dates))))
        previous = np.r_[close[0], close[:-1]]
        opening = previous * (1 + rng.normal(0, 0.004, len(dates)))
        volume = rng.integers(10000, 500000, len(dates))
        frame = pd.DataFrame({
            "ts_code": code, "stock_name": name, "trade_date": dates.strftime("%Y%m%d"),
            "open": opening, "high": np.maximum(opening, close) * 1.008,
            "low": np.minimum(opening, close) * 0.992, "close": close,
            "pre_close": previous, "change": close - previous,
            "pct_chg": (close / previous - 1) * 100,
            "vol": volume, "amount": volume * close / 10,
        })
        frames.append(frame[COLUMNS])
    return pd.concat(frames, ignore_index=True).round(4)


def write_database(frame, path, kind):
    """Never overwrite an existing database, including the user's local data."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(path)) as conn, conn:
            frame[COLUMNS].to_sql("stock_prices", conn, index=False)
            conn.execute("CREATE UNIQUE INDEX stock_date ON stock_prices(ts_code, trade_date)")
            conn.execute("CREATE TABLE dataset_metadata (kind TEXT NOT NULL)")
            conn.execute("INSERT INTO dataset_metadata VALUES (?)", (kind,))
    except Exception:
        path.unlink()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Create synthetic data without API keys")
    parser.add_argument("--db", type=Path, default=database_path(), help="New output database path")
    parser.add_argument("--start", default="20200101", help="Start date, YYYYMMDD")
    parser.add_argument("--end", default=datetime.now().strftime("%Y%m%d"), help="End date, YYYYMMDD")
    args = parser.parse_args()
    if args.db.exists():
        parser.error("目标数据库已存在。请用 --db 指定一个新路径，避免覆盖本地数据。")
    if args.demo:
        frame, kind = demo_frame(), "synthetic"
    else:
        import os
        import tushare as ts
        token = os.getenv("TUSHARE_TOKEN")
        if not token:
            parser.error("请在 .env 中设置 TUSHARE_TOKEN，或使用 --demo。")
        client = ts.pro_api(token)
        frames = []
        for code, name in STOCKS.items():
            print(f"Fetching {code}...")
            data = client.daily(ts_code=code, start_date=args.start, end_date=args.end)
            if data is None or data.empty:
                parser.error(f"{code} 返回空数据，请检查权限和日期范围。")
            data["stock_name"] = name
            frames.append(data[COLUMNS])
        frame = pd.concat(frames, ignore_index=True).drop_duplicates(["ts_code", "trade_date"])
        frame = frame.sort_values(["ts_code", "trade_date"])
        kind = "tushare"
    write_database(frame, args.db, kind)
    print(f"Created {args.db}: {len(frame)} rows; dataset={kind}")


if __name__ == "__main__":
    main()
