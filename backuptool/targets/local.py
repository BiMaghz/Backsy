import logging
import subprocess
import shutil
import os
from datetime import datetime, timezone
from pathlib import Path
from .base import BaseTarget
from .mixins import CommandGeneratorMixin
from backuptool.utils.paths import plan_paths
from backuptool.utils.helpers import SPACE_FACTOR, estimate_size_bytes, has_free_space

logger = logging.getLogger(__name__)

THROTTLE_LIST = ["nice", "-n", "15", "ionice", "-c", "3"]
THROTTLE_STR = "nice -n 15 ionice -c 3"

def _is_tool_available(name: str) -> bool:
    return shutil.which(name) is not None

def _run_rsync(cmd: list[str]) -> None:
    """Exit 24 (source files vanished mid-transfer) is normal on live systems: warn only."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 24:
        logger.warning(f"rsync: some files vanished during transfer ({cmd[-2]}). Continuing.")
    elif result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)

class LocalTarget(BaseTarget, CommandGeneratorMixin):

    def execute(self) -> Path | None:
        logger.info(f"Executing local backup for target '{self.name}'...")
        target_tmp_dir = self.tmp_dir / f"target_{self.name}"
        if target_tmp_dir.exists():
            shutil.rmtree(target_tmp_dir)
        target_tmp_dir.mkdir(parents=True)

        archive_path = None
        success = False
        try:
            plans, notes = plan_paths(self.config.get('paths', []))
            for note in notes:
                logger.warning(note)

            # Scale the disk check to the data actually being backed up (excludes honoured).
            existing = [p.src for p in plans if Path(p.src).exists()]
            estimate = estimate_size_bytes(existing, self.config.get('exclude', []))
            if estimate and not has_free_space(self.tmp_dir, int(estimate * SPACE_FACTOR), f"target '{self.name}'"):
                return None

            db_config = self.config.get('database', {})
            if db_config.get('enable'):
                logger.info(f"Starting local DB backup for '{db_config['name']}'...")
                
                db_type = db_config.get('type', '').lower()
                ext = "pgdump" if db_type == 'postgresql' else "sql"
                dump_file = target_tmp_dir / f"{db_config['name']}_backup.{ext}"
                
                cmd_result = self.build_db_dump_command(db_config, str(dump_file))

                if cmd_result:
                    cmd, pwd_env_var = cmd_result
                    dump_env = os.environ.copy()
                    if pwd_env_var:
                        dump_env[pwd_env_var] = db_config.get('password', '')
                    subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True, env=dump_env)
                    logger.info("Local DB backup completed.")
                else:
                    return None

            logger.info(f"Syncing local paths for target '{self.name}'...")
            
            rsync_base_cmd = THROTTLE_LIST + ['rsync', '-a']
            for ex in self.config.get('exclude', []):
                rsync_base_cmd.append(f"--exclude={ex}")

            for plan in plans:
                src = Path(plan.src)
                if not src.exists():
                    logger.warning(f"Local path not found: {src}")
                    continue

                dest = target_tmp_dir / plan.dest
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    # Trailing slashes: copy the directory's *contents* to exactly `dest`.
                    _run_rsync(rsync_base_cmd + [f"{src}/", f"{dest}/"])
                else:
                    _run_rsync(rsync_base_cmd + [str(src), str(dest)])

            timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')
            archive_name = f"{timestamp}_{self.name}.tar.gz"
            archive_path = self.tmp_dir / archive_name
            
            use_pigz = _is_tool_available('pigz')
            
            archive_cmd = self.build_tar_command(
                source_dir=str(target_tmp_dir),
                archive_path=str(archive_path),
                excludes=[],
                use_pigz=use_pigz,
                throttle_cmd=THROTTLE_STR
            )
            
            logger.info("Creating archive...")
            subprocess.run(archive_cmd, shell=True, check=True, capture_output=True, text=True)

            archive_size_mb = os.path.getsize(archive_path) / (1024 * 1024)
            logger.info(f"Local archive created successfully! Size: {archive_size_mb:.2f} MB")
            success = True
            return archive_path

        except subprocess.CalledProcessError as e:
            error_msg = "Unknown error"
            if hasattr(e, 'stderr') and e.stderr:
                error_msg = e.stderr if isinstance(e.stderr, str) else e.stderr.decode().strip()
            elif hasattr(e, 'output') and e.output:
                error_msg = e.output if isinstance(e.output, str) else e.output.decode().strip()
            else:
                error_msg = str(e)
            
            logger.error(f"Local backup process failed: {error_msg}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in local backup: {e}", exc_info=True)
            return None
        finally:
            if target_tmp_dir.exists():
                shutil.rmtree(target_tmp_dir)
                logger.info(f"Cleaned up temporary directory: {target_tmp_dir}")
            if not success and archive_path is not None:
                archive_path.unlink(missing_ok=True)  # never leave a partial archive behind
