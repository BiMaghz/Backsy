import shlex
import logging

from backuptool.core.config import DatabaseConfig
from backuptool.utils.compression import Compressor

logger = logging.getLogger(__name__)

class CommandGeneratorMixin:

    def build_db_dump_command(self, db_config: DatabaseConfig, output_path: str) -> tuple[str, str] | None:
        """
        Returns (cmd, pwd_env_var). The password itself is never interpolated
        into `cmd` — callers must supply it at execution time via the named
        env var (MYSQL_PWD / PGPASSWORD), which the dump tools read natively.
        Embedding it directly in the command string would leak it in plaintext
        to any local user via `ps`/`/proc/<pid>/cmdline` for the dump's duration.
        """
        db_type = (db_config.type or '').lower()
        container = db_config.container

        db_host = db_config.host
        db_port = db_config.port

        name = shlex.quote(db_config.name)
        user = shlex.quote(db_config.user)
        output = shlex.quote(str(output_path))
        container_safe = shlex.quote(container) if container else ""

        cmd = ""
        pwd_env_var = ""

        if db_type in ['mysql', 'mariadb']:
            dump_tool = 'mariadb-dump' if db_type == 'mariadb' else 'mysqldump'
            flags = f"--user={user} --single-transaction --routines --triggers"
            pwd_env_var = "MYSQL_PWD"

            if db_host:
                port_flag = f"-P {db_port}" if db_port else ""
                host_flag = f"-h {shlex.quote(db_host)}"
                cmd = f"{dump_tool} {host_flag} {port_flag} {flags} {name} > {output}"
            elif container:
                # `-e VAR` (no `=value`) forwards the value from the calling
                # process's own environment into the container.
                cmd = f"docker exec -e {pwd_env_var} {container_safe} {dump_tool} {flags} {name} > {output}"
            else:
                cmd = f"{dump_tool} {flags} {name} > {output}"

        elif db_type == 'postgresql':
            flags = f"-U {user} -d {name} -Fc"
            pwd_env_var = "PGPASSWORD"

            if container:
                cmd = f"docker exec -e {pwd_env_var} {container_safe} pg_dump {flags} > {output}"
            elif db_host:
                port_flag = f"-p {db_port}" if db_port else ""
                host_flag = f"-h {shlex.quote(db_host)}"
                cmd = f"pg_dump {host_flag} {port_flag} {flags} > {output}"
            else:
                cmd = f"pg_dump {flags} > {output}"

        else:
            logger.error(f"Unsupported database type: {db_type}")
            return None

        return cmd, pwd_env_var

    def build_tar_command(self, source_dir: str, archive_path: str, excludes: list, compressor: Compressor, throttle_cmd: str = "") -> str:
        safe_archive_path = shlex.quote(str(archive_path))
        safe_source_dir = shlex.quote(str(source_dir))

        root_folder_name = str(archive_path).split('/')[-1].removesuffix(compressor.extension)

        transform_expr = f"s,^.,{root_folder_name},S"
        transform_flag = f"--transform={shlex.quote(transform_expr)}"

        exclude_str = " ".join(f"--exclude={shlex.quote(ex)}" for ex in excludes)

        prefix = f"{throttle_cmd} " if throttle_cmd else ""

        # tar runs the compressor itself, so a failing compressor (or a tar read error) is
        # reflected in tar's exit status - unlike `tar | compressor > file`, where only the
        # last command's status survives. The compressor inherits nice/ionice from tar.
        compress_flag = f"--use-compress-program={shlex.quote(compressor.program)}"
        return f"{prefix}tar {exclude_str} {transform_flag} {compress_flag} -cf {safe_archive_path} -C {safe_source_dir} ."
