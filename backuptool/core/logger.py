import logging
import sys
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

LOG_FILE = "/var/log/Backsy.log"

def setup_logging():

    try:
        log_path = Path(LOG_FILE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        
        if not log_path.is_file() or not hasattr(log_path, 'write'):
            pass

    except PermissionError:
        print(f"Error: Permission denied to create or write to log file at {LOG_FILE}. Please check permissions.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error setting up log file at {LOG_FILE}: {e}", file=sys.stderr)
        sys.exit(1)

    log_formatter = logging.Formatter(
        '%(asctime)s [%(levelname)-8s] [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    file_handler = TimedRotatingFileHandler(
        LOG_FILE, 
        when="midnight", 
        interval=1, 
        backupCount=7,
        encoding='utf-8'
    )
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    root_logger.addHandler(console_handler)