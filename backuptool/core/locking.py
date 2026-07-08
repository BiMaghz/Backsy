import logging
import sys
import os
from pathlib import Path

logger = logging.getLogger(__name__)

class LockExistsError(Exception):
    pass

class FileLock:
    def __init__(self, lock_file_path: str, exit_on_lock: bool = True):
        self.lock_file = Path(lock_file_path)
        self.exit_on_lock = exit_on_lock

    def _is_pid_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0) 
            return True
        except OSError:
            return False
        except ValueError:
            return False

    def _handle_existing_lock(self):
        """
        Inspects an existing lock file and, if it's stale (corrupted or owned
        by a dead PID), removes it so a fresh atomic create can be attempted.
        If the lock is genuinely held by a live process, exits/raises.
        """
        try:
            pid = int(self.lock_file.read_text().strip())
        except (ValueError, OSError) as e:
            logger.warning(f"Lock file exists but is corrupted or unreadable ({e}). Cleaning up...")
            try:
                self.lock_file.unlink()
            except OSError:
                pass
            return

        if self._is_pid_running(pid):
            msg = f"Lock file exists and PID {pid} is running. Another instance is active."
            logger.warning(msg)
            if self.exit_on_lock:
                sys.exit(0)
            else:
                raise LockExistsError(msg)
        else:
            logger.warning(f"Found stale lock file from dead PID {pid}. Cleaning up...")
            try:
                self.lock_file.unlink()
            except OSError:
                pass

    def __enter__(self):
        current_pid = os.getpid()

        # Try a handful of times: two processes may race to clean up a stale
        # lock and recreate it, so a single attempt isn't enough to guarantee
        # correctness, but the O_CREAT|O_EXCL open below is what actually
        # makes acquisition atomic (only one process can win it).
        for _ in range(5):
            try:
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(current_pid).encode())
                finally:
                    os.close(fd)
                logger.info(f"Lock file created at {self.lock_file} (PID: {current_pid})")
                return self
            except FileExistsError:
                self._handle_existing_lock()
                continue
            except OSError as e:
                logger.critical(f"Failed to create lock file: {e}")
                raise

        msg = "Could not acquire lock after multiple attempts (repeated race with another instance)."
        logger.warning(msg)
        if self.exit_on_lock:
            sys.exit(0)
        else:
            raise LockExistsError(msg)

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.lock_file.exists():
                try:
                    file_pid = int(self.lock_file.read_text().strip())
                    if file_pid == os.getpid():
                        self.lock_file.unlink()
                        logger.info("Lock file removed.")
                except (ValueError, OSError):
                    self.lock_file.unlink()
        except OSError as e:
            logger.error(f"Failed to remove lock file: {e}")