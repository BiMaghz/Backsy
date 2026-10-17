import hashlib
import os
import stat
import time
import subprocess
import shutil
import logging
from pathlib import Path
from functools import wraps

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

def retry(max_retries: int = 3, delay: int = 5, backoff: int = 2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            logger = logging.getLogger(func.__module__)
            current_delay = delay
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    logger.warning(
                        f"Attempt {attempt + 1} of {max_retries} for '{func.__name__}' failed: {e}. "
                        f"Retrying in {current_delay} seconds..."
                    )
                    if attempt + 1 == max_retries:
                        logger.error(f"Function '{func.__name__}' failed after {max_retries} attempts.")
                        raise
                    time.sleep(current_delay)
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
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        
        parts = sorted(list(chunk_dir.glob(f"{file_path.name}.part-*")))
        logger.info(f"Successfully split '{file_path.name}' into {len(parts)} parts.")
        return parts
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to split file '{file_path.name}'. Return code: {e.returncode}")
        logger.error(f"Stderr: {e.stderr.strip()}")
        return []
    except FileNotFoundError as e:
        logger.error(f"Failed to run split command: {e}")
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