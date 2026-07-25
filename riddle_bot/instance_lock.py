"""Cross-platform single-instance locking for a bot database."""

from __future__ import annotations

import errno
import os
import threading
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, ClassVar, Self


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when another bot instance already owns the database lock."""


class SingleInstanceLock:
    """A non-blocking, process-wide exclusive lock backed by a ``.lock`` file."""

    _registry_guard = threading.Lock()
    _acquired_paths: ClassVar[set[str]] = set()

    def __init__(self, lock_path: str | os.PathLike[str]) -> None:
        self.lock_path = Path(lock_path)
        self._registry_key = os.path.normcase(
            os.path.abspath(os.fspath(self.lock_path))
        )
        self._file: BinaryIO | None = None
        self._acquired = False

    @classmethod
    def for_database(cls, database_path: str | os.PathLike[str]) -> SingleInstanceLock:
        """Create a lock whose path is ``<database path>.lock``."""

        database = Path(database_path)
        return cls(database.with_name(f"{database.name}.lock"))

    def acquire(self) -> Self:
        """Acquire the lock without waiting, or raise on any lock conflict."""

        with self._registry_guard:
            if self._acquired or self._registry_key in self._acquired_paths:
                raise InstanceAlreadyRunningError(
                    f"Another bot instance is already using {self.lock_path}"
                )
            # Reserve before touching the OS lock so threads in this process
            # cannot race each other between the check and acquisition.
            self._acquired_paths.add(self._registry_key)

        lock_file: BinaryIO | None = None
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self.lock_path.open("a+b")
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            self._lock_file(lock_file)
        except BaseException:
            if lock_file is not None:
                lock_file.close()
            with self._registry_guard:
                self._acquired_paths.discard(self._registry_key)
            raise

        self._file = lock_file
        self._acquired = True
        return self

    def release(self) -> None:
        """Release the lock; repeated calls are harmless."""

        if not self._acquired:
            return

        lock_file = self._file
        try:
            if lock_file is not None:
                self._unlock_file(lock_file)
        finally:
            if lock_file is not None:
                lock_file.close()
            self._file = None
            self._acquired = False
            with self._registry_guard:
                self._acquired_paths.discard(self._registry_key)

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def _lock_file(self, lock_file: BinaryIO) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if isinstance(exc, BlockingIOError) or exc.errno in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            }:
                raise InstanceAlreadyRunningError(
                    f"Another bot instance is already using {self.lock_path}"
                ) from exc
            raise

    @staticmethod
    def _unlock_file(lock_file: BinaryIO) -> None:
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
