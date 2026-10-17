import sys

if sys.version_info < (3, 10):
    sys.exit(f"Backsy requires Python 3.10 or newer (found {sys.version.split()[0]}).")
