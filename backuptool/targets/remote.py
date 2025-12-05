import logging
import shlex
from datetime import datetime, timezone
from pathlib import Path
from .base import BaseTarget
from .mixins import CommandGeneratorMixin
from fabric import Connection

logger = logging.getLogger(__name__)

THROTTLE_STR = "nice -n 15 ionice -c 3"

class RemoteTarget(BaseTarget, CommandGeneratorMixin):
    def _check_remote_space(self, connection, path: str, required_mb: int = 500) -> bool:
        try:
            safe_path = shlex.quote(path)
            result = connection.run(f"df -P -k {safe_path} | tail -1", hide=True)
            available_kb = int(result.stdout.split()[3])
            available_mb = available_kb / 1024
            
            if available_mb < required_mb:
                logger.error(f"Low remote disk space on '{path}': {available_mb:.2f}MB available, {required_mb}MB required.")
                return False
            return True
        except Exception as e:
            logger.warning(f"Could not check remote disk space on '{path}': {e}. Proceeding with caution.")
            return True

    def execute(self) -> Path | None:
        logger.info(f"Executing remote backup for target '{self.name}'...")
        
        connect_kwargs = {}
        auth_config = self.config.get('auth', {})
        if auth_config.get('method') == 'key':
            connect_kwargs['key_filename'] = auth_config.get('key_path')
        elif auth_config.get('method') == 'password':
            connect_kwargs['password'] = auth_config.get('password')

        remote_cleanup_paths = []
        
        try:
            with Connection(
                host=self.config['host'], user=self.config.get('user', 'root'),
                port=self.config.get('port', 22), connect_kwargs=connect_kwargs
            ) as c:
                logger.info(f"Successfully connected to {c.host}")
                
                if not self._check_remote_space(c, "/tmp", required_mb=500):
                    return None

                timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')
                remote_staging_dir = f"/tmp/backup_staging_{self.name}_{timestamp}"
                c.run(f"mkdir -p {remote_staging_dir}", hide=True)
                remote_cleanup_paths.append(remote_staging_dir)
                
                db_config = self.config.get('database', {})
                if db_config.get('enable'):
                    logger.info("Remote Docker DB backup is enabled for this target.")
                    db_type = db_config.get('type', '').lower()
                    ext = "pgdump" if db_type == 'postgresql' else "sql"
                    
                    dump_filename = f"{db_config['name']}_backup.{ext}"
                    remote_dump_path = f"{remote_staging_dir}/{dump_filename}"
                    
                    cmd = self.build_db_dump_command(db_config, remote_dump_path)
                    
                    if cmd:
                        c.run(cmd, hide=True)
                        logger.info(f"Remote database dump created in staging directory: {dump_filename}")

                for path_entry in self.config.get('paths', []):
                    if ':' in path_entry:
                        src, alias = path_entry.split(':', 1)
                        dest = f"{remote_staging_dir}/{shlex.quote(alias)}"
                        c.run(f"mkdir -p {dest}", hide=True)
                        c.run(f"{THROTTLE_STR} rsync -a {shlex.quote(src)} {dest}", hide=True, warn=True)
                    else:
                        c.run(f"{THROTTLE_STR} rsync -aR {shlex.quote(path_entry)} {remote_staging_dir}/", hide=True, warn=True)

                archive_name = f"{timestamp}_{self.name}.tar.gz"
                remote_archive_path = f"/tmp/{archive_name}"
                remote_cleanup_paths.append(remote_archive_path)

                use_pigz = c.run("command -v pigz", hide=True, warn=True).ok
                
                archive_cmd = self.build_tar_command(
                    source_dir=remote_staging_dir,
                    archive_path=remote_archive_path,
                    excludes=self.config.get('exclude', []),
                    use_pigz=use_pigz,
                    throttle_cmd=THROTTLE_STR
                )
                
                logger.info("Running remote command to create archive.")
                c.run(archive_cmd, hide=True)

                size_result = c.run(f"stat -c %s {remote_archive_path}", hide=True)
                remote_size_bytes = int(size_result.stdout.strip())
                remote_size_mb = remote_size_bytes / (1024 * 1024)

                local_archive_path = self.tmp_dir / archive_name
                logger.info(f"Downloading archive ({remote_size_mb:.2f} MB) from {c.host}:{remote_archive_path}")
                c.get(remote_archive_path, str(local_archive_path))

                logger.info(f"Remote archive downloaded successfully! Size: {remote_size_mb:.2f} MB")
                return local_archive_path

        except Exception as e:
            logger.error(f"Failed to perform remote backup for '{self.name}': {e}", exc_info=True)
            return None
        finally:
            if remote_cleanup_paths:
                logger.info("Cleaning up remote temporary files...")
                with Connection(
                    host=self.config['host'], user=self.config.get('user', 'root'),
                    port=self.config.get('port', 22), connect_kwargs=connect_kwargs
                ) as c:
                    for f_path in remote_cleanup_paths:
                        c.run(f"rm -rf {shlex.quote(f_path)}", hide=True, warn=True)