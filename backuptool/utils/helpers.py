import hashlib
import os
import stat
import random
import time
import subprocess
import shutil
import logging
from pathlib import Path
from functools import wraps

import requests

logger = logging.getLogger(__name__)

def ensure_private_dir(path: Path) -> None:
    """
    Create `path` with mode 0700, or verify that an existing one is a real
    directory (not a symlink), owned by the current user, and not accessible to
    others. Protects the working directory holding plaintext dumps/archives.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass

    st = os.lstat(path)  # lstat: do not follow a planted symlink
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"'{path}' exists but is not a real directory (symlink?). Refusing to use it.")
    if st.st_uid != os.geteuid():
        raise RuntimeError(f"'{path}' is owned by uid {st.st_uid}, expected {os.geteuid()}. Refusing to use it.")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)

# Peak local usage for a target is the staged copy plus the archive built from it (the staging
# copy is deleted before encryption). Archives are normally smaller than their sources, so 1.5x.
SPACE_FACTOR = 1.5

def sum_du_output(output: str) -> int:
    """Sums the byte counts in `du -sb` output (lines of '<bytes>\t<path>')."""
    total = 0
    for line in output.splitlines():
        fields = line.split(None, 1)
        if fields and fields[0].isdigit():
            total += int(fields[0])
    return total

def estimate_size_bytes(paths: list[str], excludes: list[str]) -> int | None:
    """Size of `paths` honouring `excludes`; None if it cannot be determined (no `du`)."""
    if not paths:
        return 0
    cmd = ["du", "-sb", *[f"--exclude={e}" for e in excludes], "--", *paths]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    # du exits 1 when some entries are unreadable but still reports everything else.
    return sum_du_output(result.stdout)

def has_free_space(path: Path, required_bytes: int, label: str) -> bool:
    free = shutil.disk_usage(path).free
    if free < required_bytes:
        logger.error(
            f"Not enough disk space for {label}: {free / 1024**2:.0f} MB free in '{path}', "
            f"about {required_bytes / 1024**2:.0f} MB needed."
        )
        return False
    return True

def calculate_checksum(file_path: str) -> str:
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except FileNotFoundError:
        logging.error(f"Checksum calculation failed: File not found at {file_path}")
        return ""

_TRANSIENT_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
_TRANSIENT_S3_CODES = {"RequestTimeout", "Throttling", "ThrottlingException", "SlowDown", "InternalError", "ServiceUnavailable"}
_MAX_RETRY_AFTER = 120

def is_transient_error(exc: BaseException) -> bool:
    """
    True for failures worth retrying: network errors, timeouts, throttling and 5xx.
    Authentication/validation failures (other 4xx) will not fix themselves, so they fail fast.
    """
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status is None or status in _TRANSIENT_HTTP_STATUS
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                        requests.exceptions.ChunkedEncodingError, ConnectionError, TimeoutError)):
        return True

    # boto3 wraps the underlying ClientError of a failed upload in S3UploadFailedError.
    if type(exc).__name__ == "S3UploadFailedError" and exc.__context__ is not None:
        return is_transient_error(exc.__context__)

    try:
        from botocore.exceptions import ClientError, ConnectionError as BotoConnectionError, HTTPClientError
    except ImportError:
        return False
    if isinstance(exc, (BotoConnectionError, HTTPClientError)):
        return True
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = exc.response.get("Error", {}).get("Code", "")
        return status in _TRANSIENT_HTTP_STATUS or code in _TRANSIENT_S3_CODES
    return False

def _retry_after_seconds(exc: BaseException) -> float:
    """Server-requested wait (HTTP Retry-After header or Telegram's parameters.retry_after) on 429."""
    response = getattr(exc, "response", None)
    if response is None or getattr(response, "status_code", None) != 429:
        return 0
    try:
        wait = float(response.headers.get("Retry-After") or 0)
        if not wait:
            wait = float(response.json().get("parameters", {}).get("retry_after", 0))
    except (ValueError, TypeError, AttributeError):
        return 0
    return min(wait, _MAX_RETRY_AFTER)

def retry(max_retries: int = 3, delay: int = 5, backoff: int = 2, retry_on=is_transient_error):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            logger = logging.getLogger(func.__module__)
            current_delay = delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if not retry_on(e):
                        logger.error(f"'{func.__name__}' failed with a non-retryable error: {e}")
                        raise
                    if attempt + 1 == max_retries:
                        logger.error(f"Function '{func.__name__}' failed after {max_retries} attempts: {e}")
                        raise
                    wait = max(current_delay, _retry_after_seconds(e)) * random.uniform(0.8, 1.2)
                    logger.warning(
                        f"Attempt {attempt + 1} of {max_retries} for '{func.__name__}' failed: {e}. "
                        f"Retrying in {wait:.0f} seconds..."
                    )
                    time.sleep(wait)
                    current_delay *= backoff
        return wrapper
    return decorator

def split_file(file_path: Path, chunk_size_mb: int) -> list[Path]:
    if not shutil.which("split"):
        logger.error("The 'split' command is not available on this system. Cannot split files.")
        return []

    chunk_dir = file_path.parent / f"{file_path.name}_parts"
    chunk_dir.mkdir(exist_ok=True)
    
    chunk_size_bytes = f"{chunk_size_mb}M"
    prefix = chunk_dir / f"{file_path.name}.part-"
    
    cmd = [
        'split',
        '-b', chunk_size_bytes,
        '--numeric-suffixes=1',
        '--suffix-length=2',
        str(file_path),
        str(prefix)
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        
        parts = sorted(list(chunk_dir.glob(f"{file_path.name}.part-*")))
        logger.info(f"Successfully split '{file_path.name}' into {len(parts)} parts.")
        return parts
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to split file '{file_path.name}'. Return code: {e.returncode}")
        logger.error(f"Stderr: {e.stderr.strip()}")
        shutil.rmtree(chunk_dir, ignore_errors=True)
        return []
    except FileNotFoundError as e:
        logger.error(f"Failed to run split command: {e}")
        shutil.rmtree(chunk_dir, ignore_errors=True)
        return []

def cleanup_files(file_parts: list[Path]):
    if not file_parts:
        return
    
    try:
        parent_dir = file_parts[0].parent
        if parent_dir.exists() and parent_dir.name.endswith("_parts"):
            shutil.rmtree(parent_dir)
            logger.info(f"Cleaned up temporary split directory: {parent_dir}")
    except Exception as e:
        logger.warning(f"Could not clean up split files: {e}")