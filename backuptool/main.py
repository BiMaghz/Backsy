import logging
import os
import sys
from backuptool.core.logger import setup_logging
from backuptool.core.config import load_config
from backuptool.backup_manager import BackupManager
from backuptool.core.locking import FileLock, LockExistsError
from backuptool.core.signals import GracefulKiller
from backuptool.utils.notifications import send_fatal_alert

def main():
    # Everything we create (dumps, archives, lock, logs) is private to the owner by default.
    os.umask(0o077)
    setup_logging()
    killer = GracefulKiller()
    config = None
    success = False

    try:
        with FileLock("/tmp/Backsy.lock", exit_on_lock=True):
            config = load_config()
            manager = BackupManager(config, killer)
            success = manager.run()

    except LockExistsError as e:
        logging.warning(str(e))
        sys.exit(1)
    except (FileNotFoundError, KeyError) as e:
        error_message = f"Configuration error: {e}"
        logging.critical(error_message, exc_info=True)
        send_fatal_alert(error_message, config=None)
        sys.exit(1)
        
    except Exception as e:
        error_message = f"A fatal, unexpected error occurred: {e}"
        logging.critical(error_message, exc_info=True)
        send_fatal_alert(error_message, config=config)
        sys.exit(1)

    if success:
        logging.info("Backup process completed successfully.")
    else:
        logging.error("Backup process finished with errors.")
        sys.exit(1)

if __name__ == "__main__":
    main()
