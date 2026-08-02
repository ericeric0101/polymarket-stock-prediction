from __future__ import annotations

import unittest

from polymarket_stock.storage.writer import JournalWriter


class JournalWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_drains_operations_in_submission_order(self) -> None:
        completed: list[str] = []
        writer = JournalWriter(on_error=lambda _error: self.fail("unexpected writer error"))
        await writer.start()
        self.assertTrue(writer.submit(lambda: completed.append("first")))
        self.assertTrue(writer.submit(lambda: completed.append("second")))
        await writer.drain()
        await writer.close()
        self.assertEqual(completed, ["first", "second"])

    async def test_writer_reports_operation_errors_and_continues(self) -> None:
        failures: list[Exception] = []
        completed: list[str] = []
        writer = JournalWriter(on_error=failures.append)
        await writer.start()
        writer.submit(lambda: (_ for _ in ()).throw(RuntimeError("write failure")))
        writer.submit(lambda: completed.append("after-error"))
        await writer.drain()
        await writer.close()
        self.assertEqual([str(error) for error in failures], ["write failure"])
        self.assertEqual(completed, ["after-error"])


if __name__ == "__main__":
    unittest.main()
