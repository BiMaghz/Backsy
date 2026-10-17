import logging
import subprocess
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

def encrypt_file(file_path: Path, password: str) -> Path | None:
    if not shutil.which("gpg"):
        logger.critical("GPG is not installed. Cannot encrypt backup.")
        return None

    encrypted_path = file_path.with_suffix(file_path.suffix + ".gpg")
    
    # Command breakdown:
    # --batch: Non-interactive mode
    # --yes: Overwrite output if exists
    # --passphrase-fd 0: Read password from stdin (Secure)
    # --symmetric: Use symmetric encryption
    # --cipher-algo AES256: Use strong encryption standard
    # --compress-algo none: input is already gzip-compressed; recompressing only burns CPU
    cmd = [
        "gpg", "--batch", "--yes",
        "--passphrase-fd", "0",
        "--symmetric",
        "--cipher-algo", "AES256",
        "--compress-algo", "none",
        "-o", str(encrypted_path),
        str(file_path)
    ]

    try:
        logger.info(f"Encrypting '{file_path.name}'...")
        subprocess.run(
            cmd,
            input=password.encode(),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
        
        if encrypted_path.exists() and encrypted_path.stat().st_size > 0:
            logger.info(f"Encryption successful: {encrypted_path.name}")
            return encrypted_path
        else:
            logger.error("Encryption failed: Output file is empty or missing.")
            encrypted_path.unlink(missing_ok=True)
            return None

    except subprocess.CalledProcessError as e:
        logger.error(f"GPG encryption failed: {e.stderr.decode().strip()}")
        encrypted_path.unlink(missing_ok=True)  # don't leave a truncated ciphertext behind
        return None
    except Exception as e:
        logger.error(f"Unexpected error during encryption: {e}")
        encrypted_path.unlink(missing_ok=True)
        return None