import io
import logging
import posixpath
import shlex
from datetime import datetime, timezone
from pathlib import Path
from .base import BaseTarget
from .mixins import CommandGeneratorMixin
from backuptool.core.config import KNOWN_HOSTS_PATH
from backuptool.utils.paths import plan_paths
import paramiko
from fabric import Connection

logger = logging.getLogger(__name__)

THROTTLE_STR = "nice -n 15 ionice -c 3"

class RemoteTarget(BaseTarget, CommandGeneratorMixin):
    def _connection(self, connect_kwargs: dict) -> Connection:
        conn = Connection(
            host=self.config['host'], user=self.config.get('user', 'root'),
            port=self.config.get('port', 22), connect_kwargs=connect_kwargs
        )
        # Fabric's default silently accepts unknown host keys. Verify against known hosts
        # (~/.ssh/known_hosts plus our own file) and refuse anything unrecognised, otherwise
        # an on-path attacker could capture the DB password and the whole backup.
        if KNOWN_HOSTS_PATH.exists():
            conn.client.load_host_keys(str(KNOWN_HOSTS_PATH))
        conn.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        return conn

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

    def _remove_remote_dir(self, conn, path: str, connect_kwargs: dict) -> None:
        """Removes the remote work dir; retries on a fresh connection if the original one died."""
        cmd = f"rm -rf {shlex.quote(path)}"
        logger.info("Cleaning up remote temporary files...")
        try:
            conn.run(cmd, hide=True)
            return
        except Exception as e:
            logger.warning(f"Remote cleanup over the existing connection failed ({e}); retrying with a new one...")
        try:
            with self._connection(connect_kwargs) as fresh:
                fresh.run(cmd, hide=True)
        except Exception as e:
            logger.error(
                f"Could not remove '{path}' on {self.config['host']}: {e}. "
                f"It may contain plaintext backup data - remove it manually."
            )

    def _stage_paths(self, c, staging_dir: str, exclude_args: str) -> None:
        plans, notes = plan_paths(self.config.get('paths', []))
        for note in notes:
            logger.warning(note)

        for plan in plans:
            q_src = shlex.quote(plan.src)
            kind = c.run(
                f"if [ -d {q_src} ]; then echo dir; elif [ -e {q_src} ]; then echo file; else echo missing; fi",
                hide=True
            ).stdout.strip()
            if kind == "missing":
                logger.warning(f"Remote path not found: {plan.src}")
                continue

            dest = f"{staging_dir}/{plan.dest}"
            if kind == "dir":
                # Trailing slashes: copy the directory's *contents* to exactly `dest`.
                src_arg, dest_arg = plan.src + "/", dest + "/"
            else:
                src_arg, dest_arg = plan.src, dest

            result = c.run(
                f"mkdir -p {shlex.quote(posixpath.dirname(dest))} && "
                f"{THROTTLE_STR} rsync -a {exclude_args} {shlex.quote(src_arg)} {shlex.quote(dest_arg)}",
                hide=True, warn=True
            )
            if result.exited == 24:
                logger.warning(f"rsync: some files vanished during transfer ({plan.src}). Continuing.")
            elif result.exited != 0:
                raise RuntimeError(f"rsync failed for '{plan.src}' (exit {result.exited}): {result.stderr.strip()}")

    def execute(self) -> Path | None:
        logger.info(f"Executing remote backup for target '{self.name}'...")
        
        connect_kwargs = {}
        auth_config = self.config.get('auth', {})
        if auth_config.get('method') == 'key':
            connect_kwargs['key_filename'] = auth_config.get('key_path')
        elif auth_config.get('method') == 'password':
            connect_kwargs['password'] = auth_config.get('password')

        # Applied while copying, so excluded data (e.g. a DB data dir) is never staged.
        exclude_args = " ".join(f"--exclude={shlex.quote(ex)}" for ex in self.config.get('exclude', []))

        try:
            with self._connection(connect_kwargs) as c:
                logger.info(f"Successfully connected to {c.host}")
                
                if not self._check_remote_space(c, "/tmp", required_mb=500):
                    return None

                timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')
                # mktemp -d creates an unpredictable directory with mode 0700, so the staged
                # data and the archive are never readable by other users on the remote host.
                remote_work_dir = c.run("mktemp -d /tmp/backsy.XXXXXXXX", hide=True).stdout.strip()
                if not remote_work_dir.startswith("/tmp/backsy."):
                    raise RuntimeError(f"Unexpected mktemp output: {remote_work_dir!r}")

                try:
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

                    self._stage_paths(c, remote_staging_dir, exclude_args)

                    archive_name = f"{timestamp}_{self.name}.tar.gz"
                    # Sibling of the staging dir (inside the private work dir), so tar never packs itself.
                    remote_archive_path = f"{remote_work_dir}/{archive_name}"

                    use_pigz = c.run("command -v pigz", hide=True, warn=True).ok
                    
                    archive_cmd = self.build_tar_command(
                        source_dir=remote_staging_dir,
                        archive_path=remote_archive_path,
                        excludes=[],  # already applied by rsync
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
                    try:
                        c.get(remote_archive_path, str(local_archive_path))
                    except Exception:
                        local_archive_path.unlink(missing_ok=True)  # no partial download left behind
                        raise

                    logger.info(f"Remote archive downloaded successfully! Size: {remote_size_mb:.2f} MB")
                    return local_archive_path
                finally:
                    self._remove_remote_dir(c, remote_work_dir, connect_kwargs)

        except paramiko.BadHostKeyException as e:
            logger.error(
                f"HOST KEY MISMATCH for {self.config['host']}: the server presented a different key than the one "
                f"on record. This can mean a man-in-the-middle attack, or the server was reinstalled. "
                f"Verify, then update '{KNOWN_HOSTS_PATH}'. ({e})"
            )
            return None
        except paramiko.SSHException as e:
            if "known_hosts" in str(e) or "known hosts" in str(e):
                port = self.config.get('port', 22)
                logger.error(
                    f"Host key of {self.config['host']} is not trusted yet. Verify its fingerprint, then run: "
                    f"ssh-keyscan -p {port} {self.config['host']} >> {KNOWN_HOSTS_PATH}  "
                    f"(or use 'Trust SSH host keys' in the install.sh manager menu)."
                )
            else:
                logger.error(f"SSH error during remote backup for '{self.name}': {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"Failed to perform remote backup for '{self.name}': {e}", exc_info=True)
            return None
