import fcntl
import logging
import os

logger = logging.getLogger(__name__)

class LockExistsError(Exception):
    pass

class FileLock:
    """
    Single-instance guard based on flock(2). The kernel releases the lock when the
    process exits for any reason (even SIGKILL), so there is no stale-lock cleanup,
    no PID-reuse ambiguity, and no check-then-delete race.
    """
    def __init__(self, lock_file_path: str):
        self.lock_file = lock_file_path
        self._fd = None

    def __enter__(self):
        # O_NOFOLLOW: never follow a symlink planted at the lock path.
        fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 32, 0).decode(errors="ignore").strip() or "unknown"
            os.close(fd)
            raise LockExistsError(f"Another instance is already running (PID {holder}). Exiting.")
        except Exception:
            os.close(fd)
            raise

        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        logger.info(f"Lock acquired: {self.lock_file}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd is not None:
            os.close(self._fd)  # releases the flock; the file itself is left in place on purpose
            self._fd = None
            logger.info("Lock released.")
