import signal
import logging

logger = logging.getLogger(__name__)

class GracefulKiller:
    kill_now = False

    def __init__(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, signum, frame):
        logger.warning(f"\nReceived termination signal ({signum}). Stopping gracefully after current step...")
        self.kill_now = True