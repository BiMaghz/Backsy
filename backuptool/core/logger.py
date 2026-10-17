import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

from backuptool.core.redact import redact

LOG_FILE = "/var/log/Backsy.log"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

class RedactingFormatter(logging.Formatter):
    """Scrubs secrets (bot tokens, signed URLs) from every line, including tracebacks."""
    def format(self, record):
        return redact(super().format(record))

def setup_logging():
    log_formatter = RedactingFormatter(
        '%(asctime)s [%(levelname)-8s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    file_ok = True
    try:
        log_path = Path(LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding='utf-8'
        )
        file_handler.setFormatter(log_formatter)
        root_logger.addHandler(file_handler)
    except OSError as e:
        file_ok = False
        print(f"Warning: cannot write log file {LOG_FILE} ({e}). Logging to the console only.", file=sys.stderr)

    # Console output is for interactive runs (or as the fallback above). Under cron the
    # runner redirects stdout into the same log file, so a console handler there would
    # write every line twice.
    if sys.stdout.isatty() or not file_ok:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(log_formatter)
        root_logger.addHandler(console_handler)
