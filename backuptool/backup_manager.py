import logging
import os
import requests
import concurrent.futures
import multiprocessing
import shutil
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backuptool.targets.base import BaseTarget
from backuptool.targets.local import LocalTarget
from backuptool.targets.remote import RemoteTarget

from backuptool.destinations.base import BaseDestination
from backuptool.destinations.cloudflare import CloudflareDestination
from backuptool.destinations.telegram import TelegramDestination
from backuptool.destinations.s3 import S3Destination

from backuptool.core.crypto import encrypt_file
from backuptool.core.signals import GracefulKiller
from backuptool.core.settings import TMP_DIR

from backuptool.utils.helpers import calculate_checksum, ensure_private_dir
from backuptool.utils.notifications import send_fatal_alert

logger = logging.getLogger(__name__)

# Private (0700) working directory for plaintext dumps/archives. Deliberately not
# under /tmp: /tmp is world-writable (symlink/squatting risk for a root process)
# and is often a RAM-backed tmpfs. Override with BACKSY_TMP_DIR (see core/settings.py).
DEFAULT_TMP_DIR = TMP_DIR


@dataclass
class TargetResult:
    name: str
    ok: bool
    detail: str = ""


class BackupManager:
    def __init__(self, config: dict, killer: GracefulKiller):
        self.config = config
        self.killer = killer
        self.tmp_dir = DEFAULT_TMP_DIR
        self.targets_config = config.get('targets', {})
        self.services_config = config.get('services', {})
        self._encryption_pass = None

        self.targets = self._initialize_targets()
        self.destinations = self._initialize_destinations()

    def _initialize_targets(self) -> list[BaseTarget]:
        targets = []
        target_map = {
            'local': LocalTarget,
            'remote': RemoteTarget,
        }
        for name, config in self.targets_config.items():
            target_type = config.get('type')
            if target_type in target_map:
                logger.info(f"Initializing target '{name}' of type '{target_type}'")
                targets.append(target_map[target_type](name, config, self.tmp_dir))
            else:
                logger.warning(f"Unknown target type '{target_type}' for target '{name}'. Skipping.")
        return targets

    def _initialize_destinations(self) -> dict[str, BaseDestination]:
        destinations = {}
        dest_map = {
            'cloudflare': CloudflareDestination,
            'telegram': TelegramDestination,
            's3': S3Destination,
        }
        for name, config in self.services_config.items():
            if config.get('enable'):
                logger.info(f"Initializing destination: {name}")
                if name in dest_map:
                    destinations[name] = dest_map[name](config, self.killer)
        return destinations

    def _sweep_tmp_dir(self) -> None:
        """
        Removes anything a crashed/killed earlier run left behind (it may hold plaintext data).
        Safe because main() holds the single-instance lock before the manager runs.
        """
        for entry in self.tmp_dir.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                logger.warning(f"Removed leftover from a previous run: {entry.name}")
            except OSError as e:
                logger.warning(f"Could not remove leftover '{entry}': {e}")

    def _check_disk_space(self, path: Path, required_mb: int = 200) -> bool:
        try:
            stat = os.statvfs(path)
            free_space_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            if free_space_mb < required_mb:
                logger.error(f"Low disk space: {free_space_mb:.2f}MB available in '{path}', but {required_mb}MB required.")
                return False
            logger.info(f"Disk space check passed for '{path}': {free_space_mb:.2f}MB available.")
            return True
        except FileNotFoundError:
            logger.error(f"Disk space check failed: Path '{path}' does not exist.")
            return False

    def _send_to_destinations(self, archive_path: Path, target_name: str) -> list[str]:
        """Sends the archive everywhere. Returns the names of destinations that failed."""
        if not self.destinations:
            logger.warning("No destinations enabled. Backup archive is only available locally in temp.")
            return []

        failed: list[str] = []

        total_size_mb = archive_path.stat().st_size / (1024 * 1024)

        archive_checksum = calculate_checksum(str(archive_path))
        logger.info(f"Calculated checksum for '{archive_path.name}': {archive_checksum}")

        base_caption = (
            f"📦 *Backup Notification*\n\n"
            f"• *Target:* `{target_name}`\n"
            f"• *Time:* `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`\n"
            f"• *Archive:* `{archive_path.name}` ({total_size_mb:.2f} MB)\n"
            f"• *Archive SHA256:* `{archive_checksum}`\n"
        )

        link_caption_update = ""

        if 'cloudflare' in self.destinations:
            logger.info("--- Processing Cloudflare destination sequentially ---")
            # Guarded: a Cloudflare outage must not stop S3/Telegram from running.
            try:
                result = self.destinations['cloudflare'].send(archive_path, base_caption)
                if isinstance(result, str):
                    link_caption_update = result
                elif not result:
                    failed.append('cloudflare')
            except Exception as exc:
                logger.error(f"Destination 'cloudflare' failed: {exc}")
                failed.append('cloudflare')

        elif 's3' in self.destinations:
            s3_dest = self.destinations['s3']
            if not link_caption_update:
                url = s3_dest.get_presigned_url(archive_path.name)
                if url:
                    link_caption_update = f"\n• [S3 Direct Link]({url})"
                    logger.info("Generated S3 pre-signed URL for caption.")

        other_destinations = {n: d for n, d in self.destinations.items() if n != 'cloudflare'}

        if other_destinations:
            logger.info(f"--- Processing remaining destinations in parallel: {list(other_destinations.keys())} ---")
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(other_destinations)) as executor:
                future_to_dest = {
                    executor.submit(dest.send, archive_path, base_caption, link_caption_update): name
                    for name, dest in other_destinations.items()
                }
                for future in concurrent.futures.as_completed(future_to_dest):
                    dest_name = future_to_dest[future]
                    try:
                        if future.result() is False:
                            failed.append(dest_name)
                    except Exception as exc:
                        logger.error(f"Parallel task for '{dest_name}' generated an exception: {exc}")
                        failed.append(dest_name)

        return failed

    def _encrypt(self, archive_path: Path) -> Path | None:
        """Returns the path to upload, or None if encryption was required but failed."""
        if not self._encryption_pass:
            return archive_path

        encrypted_path = encrypt_file(archive_path, self._encryption_pass)
        if encrypted_path is None:
            logger.error("Encryption failed! Aborting upload for safety.")
            return None  # the plaintext archive is removed by _process_target's cleanup

        try:
            archive_path.unlink()
            logger.info(f"Removed unencrypted file: {archive_path.name}")
        except OSError as e:
            logger.warning(f"Failed to remove unencrypted file: {e}")
        return encrypted_path

    def _process_target(self, target: BaseTarget) -> TargetResult:
        """Full pipeline for one target (backup -> encrypt -> upload -> cleanup), run in a worker thread."""
        if self.killer.kill_now:
            return TargetResult(target.name, False, "skipped (interrupted by signal)")

        archive_path = final_path = None
        try:
            logger.info(f"===== Starting Target: {target.name} =====")
            archive_path = target.execute()
            if not (archive_path and archive_path.exists()):
                logger.error(f"Backup failed for target '{target.name}'. Skipping destinations.")
                return TargetResult(target.name, False, "backup failed")

            final_path = self._encrypt(archive_path)
            if final_path is None:
                return TargetResult(target.name, False, "encryption failed")

            failed = self._send_to_destinations(final_path, target.name)
            if failed:
                return TargetResult(target.name, False, f"upload failed: {', '.join(failed)}")
            return TargetResult(target.name, True)

        except Exception as exc:
            logger.error(f"Target '{target.name}' generated an exception during execution: {exc}", exc_info=True)
            return TargetResult(target.name, False, f"{type(exc).__name__}: {exc}")
        finally:
            for path in {archive_path, final_path}:
                if path is None:
                    continue
                try:
                    if path.exists():
                        path.unlink()
                        logger.info(f"Cleaned up temporary archive: {path}")
                except OSError as e:
                    logger.warning(f"Could not clean up archive {path}: {e}")

    def _ping_healthcheck(self, ok: bool) -> None:
        url = self.config.get('monitoring', {}).get('healthcheck_url')
        if not url:
            return

        parts = urlsplit(url)
        if '/api/push/' in parts.path:
            # Uptime Kuma push monitor: status is a query parameter.
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query['status'] = 'up' if ok else 'down'
            if not ok:
                query['msg'] = 'Backup failed'
            parts = parts._replace(query=urlencode(query))
        elif not ok:
            # Healthchecks.io style: <url>/fail signals failure.
            parts = parts._replace(path=parts.path.rstrip('/') + '/fail')

        try:
            requests.get(urlunsplit(parts), timeout=10).raise_for_status()
            logger.info(f"Healthcheck ping sent successfully ({'success' if ok else 'failure'}).")
        except Exception as e:
            logger.warning(f"Failed to send healthcheck ping: {e}")

    def _finish(self, results: list[TargetResult]) -> bool:
        failed = [r for r in results if not r.ok]
        for r in results:
            if r.ok:
                logger.info(f"✔ {r.name}: OK")
            else:
                logger.error(f"✘ {r.name}: {r.detail}")

        if self.killer.kill_now:
            logger.warning("The run was interrupted by a termination signal; some targets may have been skipped.")

        ok = not failed
        self._ping_healthcheck(ok)
        if not ok:
            lines = "\n".join(f"- {r.name}: {r.detail}" for r in failed)
            send_fatal_alert(f"Backup finished with {len(failed)} failure(s):\n{lines}", config=self.config)
        return ok

    def run(self) -> bool:
        """Runs all targets. Returns True only if every target was backed up and delivered."""
        logger.info("Backup process started for all configured targets.")
        ensure_private_dir(self.tmp_dir)
        self._sweep_tmp_dir()

        if not self._check_disk_space(self.tmp_dir):
            return self._finish([TargetResult("(all targets)", False, "low disk space in temp directory")])

        if not self.targets:
            logger.error("No valid targets configured in 'config.yml'.")
            return self._finish([TargetResult("(config)", False, "no valid targets configured")])

        self._encryption_pass = os.getenv("BACKUP_ENCRYPTION_PASSWORD")
        if self._encryption_pass:
            logger.info("🔒 Encryption is ENABLED. Archives will be encrypted with GPG.")

        cpu_count = multiprocessing.cpu_count()
        max_concurrent_backups = max(1, min(2, cpu_count // 2))

        logger.info(f"Processing {len(self.targets)} target(s) with {max_concurrent_backups} concurrent worker(s)...")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent_backups) as executor:
            results = list(executor.map(self._process_target, self.targets))

        logger.info("Backup process finished for all targets.")
        return self._finish(results)
