import logging
import subprocess
import shutil
import os
from datetime import datetime, timezone
from pathlib import Path
from .base import BaseTarget

logger = logging.getLogger(__name__)

THROTTLE_PREFIX = ["nice", "-n", "15", "ionice", "-c", "3"]

def _is_tool_available(name: str) -> bool:
    return shutil.which(name) is not None

class LocalTarget(BaseTarget):
    def _backup_database(self, db_config: dict, target_dir: Path) -> bool:
        if not db_config.get('enable', False):
            return True

        db_type = db_config.get('type', '').lower()
        container = db_config['container']
        db_name = db_config['name']
        db_pass = db_config.get('password')
        db_user = db_config.get('user')

        logger.info(f"Starting local Docker DB backup for '{db_name}' from container '{container}'...")
        try:
            if db_type in ['mysql', 'mariadb']:
                dump_tool = 'mariadb-dump' if db_type == 'mariadb' else 'mysqldump'
                backup_file = target_dir / f"{db_name}_backup.sql"
                
                cmd = [
                    'docker', 'exec', '-i', '-e', f"MYSQL_PWD={db_pass}", container,
                    dump_tool, f"--user={db_user}", '--single-transaction',
                    '--routines', '--triggers', db_name
                ]
                with open(backup_file, 'w', encoding='utf-8') as f:
                    subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, check=True, text=True)
            
            elif db_type == 'postgresql':
                temp_backup_path = f"/tmp/{db_name}_backup.pgdump"
                final_backup_file = target_dir / f"{db_name}_backup.pgdump"
                
                dump_cmd = [
                    'docker', 'exec', '-e', f"PGPASSWORD={db_pass}", container,
                    'pg_dump', f"-U{db_user}", f"-d{db_name}",
                    '--format=c', f"--file={temp_backup_path}"
                ]
                copy_cmd = ['docker', 'cp', f"{container}:{temp_backup_path}", str(final_backup_file)]
                cleanup_cmd = ['docker', 'exec', container, 'rm', temp_backup_path]
                
                subprocess.run(dump_cmd, check=True, capture_output=True, text=True)
                subprocess.run(copy_cmd, check=True, capture_output=True, text=True)
                subprocess.run(cleanup_cmd, check=False)
            
            logger.info(f"Local Docker DB backup for '{db_name}' completed successfully.")
            return True

        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            error_msg = "Unknown error"
            if hasattr(e, 'stderr') and e.stderr:
                if isinstance(e.stderr, bytes):
                    error_msg = e.stderr.decode().strip()
                else:
                    error_msg = e.stderr.strip()
            elif hasattr(e, 'output') and e.output:
                 if isinstance(e.output, bytes):
                    error_msg = e.output.decode().strip()
                 else:
                    error_msg = e.output.strip()
            else:
                error_msg = str(e)

            logger.error(f"Failed to dump local Docker database: {error_msg}")
            return False

    def execute(self) -> Path | None:
        logger.info(f"Executing local backup for target '{self.name}'...")
        target_tmp_dir = self.tmp_dir / f"target_{self.name}"
        if target_tmp_dir.exists():
            shutil.rmtree(target_tmp_dir)
        target_tmp_dir.mkdir(parents=True)

        try:
            if not self._backup_database(self.config.get('database', {}), target_tmp_dir):
                return None

            logger.info(f"Syncing local paths for target '{self.name}'...")
            
            rsync_base_cmd = THROTTLE_PREFIX + ['rsync', '-a']
            for ex in self.config.get('exclude', []):
                rsync_base_cmd.append(f"--exclude={ex}")
            
            for path_entry in self.config.get('paths', []):
                if ':' in path_entry:
                    src_path_str, alias = path_entry.split(':', 1)
                    src_path = Path(src_path_str)
                    
                    dest_path = target_tmp_dir / alias
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    if src_path.exists():
                        subprocess.run(rsync_base_cmd + [str(src_path), str(dest_path)], check=True)
                    else:
                        logger.warning(f"Local path not found: {src_path}")
                else:
                    src_path = Path(path_entry)
                    if src_path.exists():
                        cmd_with_rel = list(rsync_base_cmd)
                        cmd_with_rel.append('-R')
                        subprocess.run(cmd_with_rel + [str(src_path), str(target_tmp_dir)], check=True)
                    else:
                        logger.warning(f"Local path not found: {src_path}")

            timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')
            archive_name = f"{timestamp}_{self.name}.tar.gz"
            archive_path = self.tmp_dir / archive_name
            
            root_folder_name = archive_name.replace(".tar.gz", "")
            transform_flag = f"--transform=s,^.,{root_folder_name},S"
            
            if _is_tool_available('pigz'):
                logger.info("Using 'pigz' for fast, parallel compression.")
                
                tar_cmd = THROTTLE_PREFIX + ['tar', '-cf', '-', transform_flag, '-C', str(target_tmp_dir), '.']
                
                pigz_cmd = THROTTLE_PREFIX + ['pigz', '-9']
                
                with open(archive_path, 'wb') as f_out:
                    tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE)
                    pigz_proc = subprocess.Popen(pigz_cmd, stdin=tar_proc.stdout, stdout=f_out)
                    
                    tar_proc.stdout.close()
                    
                    pigz_proc.wait()
                    tar_proc.wait()
                    
                    if tar_proc.returncode != 0 or pigz_proc.returncode != 0:
                        raise subprocess.CalledProcessError(tar_proc.returncode or pigz_proc.returncode, "tar|pigz")

            else:
                logger.info("Pigz not found. Falling back to standard gzip.")
                cmd_list = THROTTLE_PREFIX + [
                    'tar', '-czf', str(archive_path),
                    transform_flag,
                    '-C', str(target_tmp_dir), '.'
                ]
                subprocess.run(cmd_list, check=True)

            archive_size_mb = os.path.getsize(archive_path) / (1024 * 1024)
            logger.info(f"Local archive created successfully! Size: {archive_size_mb:.2f} MB")
            return archive_path

        except subprocess.CalledProcessError as e:
            logger.error(f"Archive creation failed for '{self.name}': {e}")
            return None
        except Exception as e:
            logger.error(f"An unexpected error occurred during local target execution: {e}", exc_info=True)
            return None
        finally:
            shutil.rmtree(target_tmp_dir)
            logger.info(f"Cleaned up temporary directory: {target_tmp_dir}")