import shlex
import logging

from backuptool.core.config import DatabaseConfig

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

    def build_tar_command(self, source_dir: str, archive_path: str, excludes: list, use_pigz: bool, throttle_cmd: str = "") -> str:
        safe_archive_path = shlex.quote(str(archive_path))
        safe_source_dir = shlex.quote(str(source_dir))
        
        archive_filename = str(archive_path).split('/')[-1]
        root_folder_name = archive_filename.replace(".tar.gz", "")
        
        transform_expr = f"s,^.,{root_folder_name},S"
        transform_flag = f"--transform={shlex.quote(transform_expr)}"

        exclude_parts = [f"--exclude={shlex.quote(ex)}" for ex in excludes]
        exclude_str = " ".join(exclude_parts)

        prefix = f"{throttle_cmd} " if throttle_cmd else ""
        
        tar_base = f"{prefix}tar {exclude_str} {transform_flag} -cf - -C {safe_source_dir} ."

        if use_pigz:
            cmd = f"{tar_base} | {prefix}pigz -6 > {safe_archive_path}"
        else:
            # Same level as the pigz branch, so archive size doesn't depend on whether pigz
            # is installed. Level 6 is ~4x faster than level 9 for ~1% larger output.
            cmd = f'{prefix}tar {exclude_str} {transform_flag} --use-compress-program="gzip -6" -cf {safe_archive_path} -C {safe_source_dir} .'

        return cmd