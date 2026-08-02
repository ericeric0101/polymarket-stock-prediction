from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from polymarket_stock.storage.sqlite import database_connection


class SqliteStorageTests(unittest.TestCase):
    def test_transaction_commits_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.db"
            with database_connection(path) as connection:
                connection.execute("CREATE TABLE values_table (value TEXT)")
                connection.execute("INSERT INTO values_table VALUES ('committed')")
            with self.assertRaises(RuntimeError):
                with database_connection(path) as connection:
                    connection.execute("INSERT INTO values_table VALUES ('rolled-back')")
                    raise RuntimeError("stop")
            with database_connection(path) as connection:
                values = [row[0] for row in connection.execute("SELECT value FROM values_table")]
        self.assertEqual(values, ["committed"])


if __name__ == "__main__":
    unittest.main()
