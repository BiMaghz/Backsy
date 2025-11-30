import logging
import sys
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler

LOG_FILE = "/var/log/Backsy.log"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

def setup_logging():
    log_path = Path(LOG_FILE)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        
        if not os.access(log_path, os.W_OK):
             raise PermissionError(f"No write access to {LOG_FILE}")

    except (PermissionError, OSError) as e:
        print(f"Critical Error: Cannot create/write log file at {LOG_FILE}.\nDetails: {e}", file=sys.stderr)
        sys.exit(1)

    log_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)-8s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    file_handler = RotatingFileHandler(
        LOG_FILE, 
        maxBytes=MAX_BYTES, 
        backupCount=BACKUP_COUNT, 
        encoding='utf-8'
    )
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    root_logger.addHandler(console_handler)