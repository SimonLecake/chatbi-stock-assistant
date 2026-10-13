import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from db import read_connection, dataset_note
from prepare_data import demo_frame, write_database


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "demo.db"
        write_database(demo_frame(), self.path, "synthetic")
        self.env = patch.dict(os.environ, {"CHATBI_DB_PATH": str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_demo_is_reproducible_and_identified(self):
        self.assertTrue(demo_frame().equals(demo_frame()))
        with read_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(DISTINCT ts_code) FROM stock_prices").fetchone()[0], 7)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM stock_prices").fetchone()[0], 1827)
        self.assertIn("合成演示数据", dataset_note())

    def test_existing_database_is_preserved(self):
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            write_database(demo_frame(), self.path, "synthetic")
        self.assertEqual(before, self.path.read_bytes())

    def test_analysis_connection_rejects_writes_and_attach(self):
        for sql in ["DELETE FROM stock_prices", "DROP TABLE stock_prices",
                    "ATTACH DATABASE ':memory:' AS extra", "PRAGMA query_only=OFF"]:
            with self.subTest(sql=sql), read_connection() as conn:
                with self.assertRaises(sqlite3.DatabaseError):
                    conn.execute(sql)
        with read_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM stock_prices").fetchone()[0], 1827)

    def test_unique_stock_date(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO stock_prices SELECT * FROM stock_prices LIMIT 1")


if __name__ == "__main__":
    unittest.main()
