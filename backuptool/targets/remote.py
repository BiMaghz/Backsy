import io
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
    def _run_with_password(self, connection, cmd: str, pwd_env_var: str, password: str):
        """
        Runs `cmd` on the remote host, supplying the DB password through the
        SSH stdin channel rather than the command text. Fabric's own `env=`
        support works by prepending a literal `export VAR=value` to the
        remote command, which would still leak the password via `ps` on the
        remote host, so it isn't used here.
        """
        if not pwd_env_var:
            connection.run(cmd, hide=True)
            return

        inner = f'IFS= read -r DBPWD && export {pwd_env_var}="$DBPWD" && {cmd}'
        wrapped_cmd = f"sh -c {shlex.quote(inner)}"
        connection.run(wrapped_cmd, hide=True, in_stream=io.StringIO(password + "\n"))

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
                # mktemp -d creates an unpredictable directory with mode 0700, so the staged
                # data and the archive are never readable by other users on the remote host.
                remote_work_dir = c.run("mktemp -d /tmp/backsy.XXXXXXXX", hide=True).stdout.strip()
                remote_cleanup_paths.append(remote_work_dir)
                remote_staging_dir = f"{remote_work_dir}/stage"
                c.run(f"mkdir -m 700 {remote_staging_dir}", hide=True)
                
                db_config = self.config.get('database', {})
                if db_config.get('enable'):
                    logger.info("Remote Docker DB backup is enabled for this target.")
                    db_type = db_config.get('type', '').lower()
                    ext = "pgdump" if db_type == 'postgresql' else "sql"
                    
                    dump_filename = f"{db_config['name']}_backup.{ext}"
                    remote_dump_path = f"{remote_staging_dir}/{dump_filename}"
                    
                    cmd_result = self.build_db_dump_command(db_config, remote_dump_path)

                    if cmd_result:
                        cmd, pwd_env_var = cmd_result
                        self._run_with_password(c, cmd, pwd_env_var, db_config.get('password', ''))
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
                # Sibling of the staging dir (inside the private work dir), so tar never packs itself.
                remote_archive_path = f"{remote_work_dir}/{archive_name}"

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

                size_result = c.run(f"stat -c %s {shlex.quote(remote_archive_path)}", hide=True)
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