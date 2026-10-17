import os
from pathlib import Path

# Defaults match the installer's layout. Each can be overridden with an environment
# variable (e.g. to run a second instance or to test without touching /var or /run).
CONFIG_PATH = os.environ.get("BACKSY_CONFIG", "config.yml")          # relative to the working directory
LOG_FILE = os.environ.get("BACKSY_LOG_FILE", "/var/log/Backsy.log")
LOCK_FILE = os.environ.get("BACKSY_LOCK_FILE", "/run/Backsy.lock")
TMP_DIR = Path(os.environ.get("BACKSY_TMP_DIR", "/var/lib/Backsy/tmp"))
