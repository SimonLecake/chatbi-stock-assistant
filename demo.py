"""Run the three analysis tools offline, without calling an LLM or market API."""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

os.environ.setdefault("MPLBACKEND", "Agg")

from db import ROOT
from prepare_data import demo_frame, write_database


def main():
    output = ROOT / "demo_output"
    output.mkdir(exist_ok=True)
    previous = os.environ.get("CHATBI_DB_PATH")
    try:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "demo.db"
            write_database(demo_frame(), path, "synthetic")
            os.environ["CHATBI_DB_PATH"] = str(path)
            from app import ExcSQLTool, ARIMASTockTool, BollDetectionTool
            cases = [
                ("query", ExcSQLTool(), {"sql_input": "SELECT trade_date, close FROM stock_prices WHERE ts_code='600519.SH' AND trade_date BETWEEN '20250101' AND '20250331' ORDER BY trade_date"}),
                ("forecast", ARIMASTockTool(), {"ts_code": "600519.SH", "n": 7}),
                ("bollinger", BollDetectionTool(), {"ts_code": "002415.SZ", "start_date": "20250101", "end_date": "20251231"}),
            ]
            for name, tool, args in cases:
                result = tool.call(json.dumps(args))
                match = re.search(r"!\[[^\]]*\]\(([^)]+)\)", result)
                if not match:
                    raise RuntimeError(f"{name}: expected a chart in the tool output")
                source = ROOT / match.group(1)
                if not source.is_file():
                    raise RuntimeError(f"{name}: chart file missing")
                destination = output / f"{name}.png"
                shutil.copy2(source, destination)
                result = result.replace(match.group(1), destination.name)
                (output / f"{name}.md").write_text(
                    "> 合成演示数据，不代表真实股票行情。此演示直接调用工具，不验证 LLM 路由。\n\n" + result,
                    encoding="utf-8",
                )
                print(f"OK: {name} -> {destination}")
    finally:
        if previous is None:
            os.environ.pop("CHATBI_DB_PATH", None)
        else:
            os.environ["CHATBI_DB_PATH"] = previous


if __name__ == "__main__":
    main()
