import shlex
import logging

logger = logging.getLogger(__name__)

class CommandGeneratorMixin:

    def build_db_dump_command(self, db_config: dict, output_path: str) -> str | None:
        db_type = db_config.get('type', '').lower()
        container = db_config.get('container')

        db_host = db_config.get('host')
        db_port = db_config.get('port')

        name = shlex.quote(db_config['name'])
        user = shlex.quote(db_config['user'])
        password = shlex.quote(db_config.get('password', ''))
        output = shlex.quote(str(output_path))
        container_safe = shlex.quote(container) if container else ""

        cmd = ""

        if db_type in ['mysql', 'mariadb']:
            dump_tool = 'mariadb-dump' if db_type == 'mariadb' else 'mysqldump'
            flags = f"--user={user} --single-transaction --routines --triggers"

            if db_host:
                port_flag = f"-P {db_port}" if db_port else ""
                host_flag = f"-h {shlex.quote(db_host)}"
                cmd = f"MYSQL_PWD={password} {dump_tool} {host_flag} {port_flag} {flags} {name} > {output}"
            
            elif container:
                cmd = f"docker exec -e MYSQL_PWD={password} {container_safe} {dump_tool} {flags} {name} > {output}"
            else:
                cmd = f"MYSQL_PWD={password} {dump_tool} {flags} {name} > {output}"

        elif db_type == 'postgresql':
            flags = f"-U {user} -d {name} -Fc"

            if container:
                cmd = f"docker exec -e PGPASSWORD={password} {container_safe} pg_dump {flags} > {output}"
            elif db_host:
                port_flag = f"-p {db_port}" if db_port else ""
                host_flag = f"-h {shlex.quote(db_host)}"
                cmd = f"PGPASSWORD={password} pg_dump {host_flag} {port_flag} {flags} > {output}"
            else:
                cmd = f"PGPASSWORD={password} pg_dump {flags} > {output}"
        
        else:
            logger.error(f"Unsupported database type: {db_type}")
            return None

        return cmd

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
            cmd = f"{tar_base} | {prefix}pigz -9 > {safe_archive_path}"
        else:
            cmd = f"{prefix}tar {exclude_str} {transform_flag} -czf {safe_archive_path} -C {safe_source_dir} ."

        return cmd