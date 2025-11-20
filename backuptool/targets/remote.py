import logging
from datetime import datetime, timezone
from pathlib import Path
from .base import BaseTarget
from fabric import Connection

logger = logging.getLogger(__name__)

class RemoteTarget(BaseTarget):
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
                
                remote_staging_dir = f"/tmp/backup_staging_{self.name}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                c.run(f"mkdir -p {remote_staging_dir}", hide=True)
                remote_cleanup_paths.append(remote_staging_dir)
                
                db_config = self.config.get('database', {})
                if db_config.get('enable'):
                    logger.info("Remote Docker DB backup is enabled for this target.")
                    db_type = db_config.get('type', '').lower()
                    db_name = db_config['name']
                    container = db_config['container']
                    db_pass = db_config.get('password')
                    db_user = db_config.get('user')
                    
                    dump_filename = f"db_dump_{db_name}.sql"
                    if db_type == 'postgresql':
                        dump_filename = f"db_dump_{db_name}.pgdump"
                        
                    remote_dump_path = f"{remote_staging_dir}/{dump_filename}"
                    cmd = ""
                    
                    if db_type in ['mysql', 'mariadb']:
                        dump_tool = 'mariadb-dump' if db_type == 'mariadb' else 'mysqldump'
                        cmd = (f"docker exec -i -e MYSQL_PWD='{db_pass}' {container} {dump_tool} "
                               f"--user={db_user} --single-transaction --routines --triggers {db_name} > {remote_dump_path}")
                    elif db_type == 'postgresql':
                        cmd = (f"docker exec -i -e PGPASSWORD='{db_pass}' {container} pg_dump "
                               f"-U {db_user} -d {db_name} -Fc > {remote_dump_path}")
                    
                    if cmd:
                        c.run(cmd, hide=True)
                        logger.info(f"Remote database dump created in staging directory: {dump_filename}")

                for path_entry in self.config.get('paths', []):
                    if ':' in path_entry:
                        src_path, alias = path_entry.split(':', 1)
                        dest_path = f"{remote_staging_dir}/{alias}"
                        c.run(f"mkdir -p {dest_path}", hide=True)
                        c.run(f"rsync -a {src_path} {dest_path}", hide=True, warn=True)
                    else:
                        src_path = path_entry
                        c.run(f"rsync -aR {src_path} {remote_staging_dir}/", hide=True, warn=True)

                archive_name = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}_{self.name}.tar.gz"
                remote_archive_path = f"/tmp/{archive_name}"
                remote_cleanup_paths.append(remote_archive_path)

                root_folder_name = archive_name.replace(".tar.gz", "")
                transform_flag = f"--transform='s,^.,{root_folder_name},S'"

                exclude_str = " ".join([f"--exclude='{ex}'" for ex in self.config.get('exclude', [])])

                use_pigz_remote = c.run("command -v pigz", hide=True, warn=True).ok
                if use_pigz_remote:
                    logger.info("Using 'pigz' on remote server for compression.")
                    tar_cmd = f"tar {exclude_str} {transform_flag} -cf - -C {remote_staging_dir} . | pigz -9 > {remote_archive_path}"
                else:
                    logger.info("Pigz not found on remote. Using standard gzip.")
                    tar_cmd = f"tar {exclude_str} {transform_flag} -czf {remote_archive_path} -C {remote_staging_dir} ."
                
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
                        c.run(f"rm -rf {f_path}", hide=True, warn=True)