import os
import signal

class GracefulKiller:
    def __init__(self):
        self.kill_now = False
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, signum, frame):
        # Signal handlers must stay trivial: logging takes locks that the interrupted code
        # may already hold (deadlock). Set the flag and write straight to stderr; the
        # manager logs the interruption from normal code.
        self.kill_now = True
        try:
            os.write(2, b"\nReceived termination signal. Stopping gracefully after the current step...\n")
        except OSError:
            pass
