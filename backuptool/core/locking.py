import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

class LockExistsError(Exception):
    pass

class FileLock:
    def __init__(self, lock_file_path: str, exit_on_lock: bool = True):
        self.lock_file = Path(lock_file_path)
        self.exit_on_lock = exit_on_lock

    def _handle_existing_lock(self):
        msg = f"Lock file '{self.lock_file}' exists. Another instance may be running."
        logger.warning(msg)
        if self.exit_on_lock:
            sys.exit(0) 
        else:
            raise LockExistsError(msg)

    def __enter__(self):
        if self.lock_file.exists():
            self._handle_existing_lock()
            return self 
        try:
            self.lock_file.touch()
            logger.info(f"Lock file created at {self.lock_file}")
        except OSError as e:
            logger.critical(f"Failed to create lock file: {e}")
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.lock_file.exists():
                self.lock_file.unlink()
                logger.info("Lock file removed.")
        except OSError as e:
            logger.error(f"Failed to remove lock file: {e}")
