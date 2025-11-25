import logging
import shlex
from datetime import datetime, timezone
from pathlib import Path
from .base import BaseTarget
from fabric import Connection

logger = logging.getLogger(__name__)

THROTTLE_CMD = "nice -n 15 ionice -c 3"

class RemoteTarget(BaseTarget):
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
                    db_name = shlex.quote(db_config['name'])
                    container = shlex.quote(db_config['container'])
                    db_pass_safe = shlex.quote(db_config.get('password'))
                    db_user_safe = shlex.quote(db_config.get('user'))
                    
                    dump_filename = f"db_dump_{db_config['name']}.sql"
                    if db_type == 'postgresql':
                        dump_filename = f"db_dump_{db_config['name']}.pgdump"
                        
                    remote_dump_path = f"{remote_staging_dir}/{dump_filename}"
                    cmd = ""
                    
                    if db_type in ['mysql', 'mariadb']:
                        cmd = (f"docker exec -i -e MYSQL_PWD={db_pass_safe} {container} mysqldump "
                               f"--user={db_user_safe} --single-transaction --routines --triggers {db_name} > {remote_dump_path}")
                        tool = 'mariadb-dump' if db_type == 'mariadb' else 'mysqldump'
                        cmd = (f"docker exec -i -e MYSQL_PWD={db_pass_safe} {container} {tool} "
                               f"--user={db_user_safe} --single-transaction --routines --triggers {db_name} > {remote_dump_path}")

                    elif db_type == 'postgresql':
                        cmd = (f"docker exec -i -e PGPASSWORD={db_pass_safe} {container} pg_dump "
                               f"-U {db_user_safe} -d {db_name} -Fc > {remote_dump_path}")
                    
                    if cmd:
                        c.run(cmd, hide=True)
                        logger.info(f"Remote database dump created in staging directory: {dump_filename}")

                for path_entry in self.config.get('paths', []):
                    if ':' in path_entry:
                        src_path_str, alias = path_entry.split(':', 1)
                        src_safe = shlex.quote(src_path_str)
                        alias_safe = shlex.quote(alias)
                        
                        dest_path = f"{remote_staging_dir}/{alias_safe}"
                        c.run(f"mkdir -p {dest_path}", hide=True)
                        c.run(f"{THROTTLE_CMD} rsync -a {src_safe} {dest_path}", hide=True, warn=True)
                    else:
                        src_safe = shlex.quote(path_entry)
                        c.run(f"{THROTTLE_CMD} rsync -aR {src_safe} {remote_staging_dir}/", hide=True, warn=True)

                archive_name = f"{timestamp}_{self.name}.tar.gz"
                remote_archive_path = f"/tmp/{archive_name}"
                remote_cleanup_paths.append(remote_archive_path)

                root_folder_name = archive_name.replace(".tar.gz", "")
                transform_flag = f"--transform='s,^.,{root_folder_name},S'"

                exclude_list = []
                for ex in self.config.get('exclude', []):
                    exclude_list.append(f"--exclude={shlex.quote(ex)}")
                exclude_str = " ".join(exclude_list)

                use_pigz_remote = c.run("command -v pigz", hide=True, warn=True).ok
                if use_pigz_remote:
                    logger.info("Using 'pigz' on remote server for compression.")
                    tar_cmd = f"{THROTTLE_CMD} tar {exclude_str} {transform_flag} -cf - -C {remote_staging_dir} . | {THROTTLE_CMD} pigz -9 > {remote_archive_path}"
                else:
                    logger.info("Pigz not found on remote. Using standard gzip.")
                    tar_cmd = f"{THROTTLE_CMD} tar {exclude_str} {transform_flag} -czf {remote_archive_path} -C {remote_staging_dir} ."
                
                logger.info("Running remote command to create archive.")
                c.run(tar_cmd, hide=True)

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