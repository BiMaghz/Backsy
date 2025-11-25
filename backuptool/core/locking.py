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
        try:
            pid = int(self.lock_file.read_text().strip())
            
            if self._is_pid_running(pid):
                msg = f"Lock file exists and PID {pid} is running. Another instance is active."
                logger.warning(msg)
                if self.exit_on_lock:
                    sys.exit(0)
                else:
                    raise LockExistsError(msg)
            else:
                logger.warning(f"Found stale lock file from dead PID {pid}. Cleaning up...")
                self.lock_file.unlink()
                return True

        except (ValueError, OSError) as e:
            logger.warning(f"Lock file exists but is corrupted or unreadable ({e}). Cleaning up...")
            try:
                self.lock_file.unlink()
            except OSError:
                pass
            return True

    def __enter__(self):
        if self.lock_file.exists():
            self._handle_existing_lock()

        try:
            current_pid = os.getpid()
            self.lock_file.write_text(str(current_pid))
            logger.info(f"Lock file created at {self.lock_file} (PID: {current_pid})")
        except OSError as e:
            logger.critical(f"Failed to create lock file: {e}")
            raise
        return self

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