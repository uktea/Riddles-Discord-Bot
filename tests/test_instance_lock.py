import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from riddle_bot.instance_lock import (
    InstanceAlreadyRunningError,
    SingleInstanceLock,
)


class SingleInstanceLockTests(unittest.TestCase):
    def test_acquires_database_lock_and_keeps_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "riddles.sqlite3"
            lock = SingleInstanceLock.for_database(database)

            with lock as acquired:
                self.assertIs(acquired, lock)
                self.assertEqual(
                    lock.lock_path, Path(temp_dir) / "riddles.sqlite3.lock"
                )
                self.assertTrue(lock.lock_path.exists())

            self.assertTrue(lock.lock_path.exists())

    def test_rejects_second_lock_for_same_database_in_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "riddles.sqlite3"
            first = SingleInstanceLock.for_database(database)
            second = SingleInstanceLock.for_database(database)

            with first, self.assertRaises(InstanceAlreadyRunningError):
                second.acquire()

    def test_can_reacquire_after_idempotent_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "riddles.sqlite3"
            first = SingleInstanceLock.for_database(database)
            second = SingleInstanceLock.for_database(database)

            first.acquire()
            first.release()
            first.release()

            with second:
                self.assertTrue(second.lock_path.exists())

    def test_rejects_lock_held_by_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "riddles.sqlite3"
            child_code = (
                "import sys\n"
                "from riddle_bot.instance_lock import "
                "InstanceAlreadyRunningError, SingleInstanceLock\n"
                f"lock = SingleInstanceLock.for_database({str(database)!r})\n"
                "try:\n"
                "    lock.acquire()\n"
                "except InstanceAlreadyRunningError:\n"
                "    raise SystemExit(0)\n"
                "else:\n"
                "    lock.release()\n"
                "    raise SystemExit(1)\n"
            )

            with SingleInstanceLock.for_database(database):
                completed = subprocess.run(
                    [sys.executable, "-c", child_code],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(
                completed.returncode,
                0,
                msg=completed.stderr or completed.stdout,
            )


if __name__ == "__main__":
    unittest.main()
