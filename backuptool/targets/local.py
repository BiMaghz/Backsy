import logging
import subprocess
import shutil
import os
from datetime import datetime, timezone
from pathlib import Path
from .base import BaseTarget
from .mixins import CommandGeneratorMixin

logger = logging.getLogger(__name__)

THROTTLE_LIST = ["nice", "-n", "15", "ionice", "-c", "3"]
THROTTLE_STR = "nice -n 15 ionice -c 3"

def _is_tool_available(name: str) -> bool:
    return shutil.which(name) is not None

class LocalTarget(BaseTarget, CommandGeneratorMixin):

    def execute(self) -> Path | None:
        logger.info(f"Executing local backup for target '{self.name}'...")
        target_tmp_dir = self.tmp_dir / f"target_{self.name}"
        if target_tmp_dir.exists():
            shutil.rmtree(target_tmp_dir)
        target_tmp_dir.mkdir(parents=True)

        try:
            db_config = self.config.get('database', {})
            if db_config.get('enable'):
                logger.info(f"Starting local DB backup for '{db_config['name']}'...")
                
                db_type = db_config.get('type', '').lower()
                ext = "pgdump" if db_type == 'postgresql' else "sql"
                dump_file = target_tmp_dir / f"{db_config['name']}_backup.{ext}"
                
                cmd = self.build_db_dump_command(db_config, str(dump_file))
                
                if cmd:
                    subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
                    logger.info("Local DB backup completed.")
                else:
                    return None

            logger.info(f"Syncing local paths for target '{self.name}'...")
            
            rsync_base_cmd = THROTTLE_LIST + ['rsync', '-a']
            for ex in self.config.get('exclude', []):
                rsync_base_cmd.append(f"--exclude={ex}")
            
            for path_entry in self.config.get('paths', []):
                if ':' in path_entry:
                    src_str, alias = path_entry.split(':', 1)
                    src = Path(src_str)
                    dest = target_tmp_dir / alias
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    
                    if src.exists():
                        subprocess.run(rsync_base_cmd + [str(src), str(dest)], check=True)
                    else:
                        logger.warning(f"Local path not found: {src}")
                else:
                    src = Path(path_entry)
                    if src.exists():
                        cmd_with_rel = list(rsync_base_cmd)
                        cmd_with_rel.append('-R')
                        subprocess.run(cmd_with_rel + [str(src), str(target_tmp_dir)], check=True)
                    else:
                        logger.warning(f"Local path not found: {src}")

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