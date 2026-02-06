from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path


class FileLock:
    """Cross-platform advisory lock using a sidecar .lock file."""

    def __init__(self, target_path: str, timeout_sec: float = 5.0, poll_sec: float = 0.05):
        self.lock_path = Path(f"{target_path}.lock")
        self.timeout_sec = timeout_sec
        self.poll_sec = poll_sec
        self._fh = None
        self._locked = False

    def __enter__(self) -> "FileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.lock_path.open("a+")
        start = time.time()

        while True:
            try:
                self._acquire()
                self._locked = True
                self._write_lock_metadata()
                return self
            except OSError:
                if (time.time() - start) >= self.timeout_sec:
                    raise TimeoutError(
                        f"Could not acquire lock: {self.lock_path} "
                        f"after {self.timeout_sec:.2f}s. lock_file_present={self.lock_path.exists()}"
                    )
                time.sleep(self.poll_sec)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is None:
            return

        try:
            self._release()
        finally:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def _acquire(self) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(self) -> None:
        if not self._locked or self._fh is None:
            return

        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except PermissionError:
            if os.name != "nt":
                raise
        except OSError:
            if os.name != "nt":
                raise
        finally:
            self._locked = False

    def _write_lock_metadata(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = f"pid={os.getpid()} acquired_utc={now_iso}\n"
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(payload)
        self._fh.flush()
