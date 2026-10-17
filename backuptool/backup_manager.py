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

from backuptool.destinations.base import BaseDestination, SendResult
from backuptool.destinations.registry import DESTINATION_TYPES

from backuptool.core.config import AppConfig
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
    def __init__(self, config: AppConfig | dict, killer: GracefulKiller):
        self.config = config if isinstance(config, AppConfig) else AppConfig.model_validate(config)
        self.killer = killer
        self.tmp_dir = DEFAULT_TMP_DIR
        self._encryption_pass = None

        self.targets = self._initialize_targets()
        self.destinations = self._initialize_destinations()

    def _initialize_targets(self) -> list[BaseTarget]:
        target_map = {
            'local': LocalTarget,
            'remote': RemoteTarget,
        }
        targets = []
        for name, target_config in self.config.targets.items():
            logger.info(f"Initializing target '{name}' of type '{target_config.type}'")
            targets.append(target_map[target_config.type](name, target_config, self.tmp_dir, self.config.compression))
        return targets

    def _initialize_destinations(self) -> dict[str, BaseDestination]:
        destinations = {}
        for name, destination_class in DESTINATION_TYPES.items():
            service_config = getattr(self.config.services, name)
            if service_config.enable:
                logger.info(f"Initializing destination: {name}")
                destinations[name] = destination_class(service_config, self.killer)
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

    def _deliver(self, name: str, destination: BaseDestination, archive_path: Path,
                 base_caption: str, link_caption: str) -> SendResult:
        # Destinations report failures themselves; this only guards against a bug in one of them.
        try:
            return destination.send(archive_path, base_caption, link_caption)
        except Exception as exc:
            logger.error(f"Destination '{name}' raised unexpectedly: {exc}", exc_info=True)
            return SendResult(False, detail=f"{type(exc).__name__}: {exc}")

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

        # Destinations that produce a download link (Cloudflare, S3 with generate_link) go first,
        # one at a time, so the link - which only exists after a *successful* upload - can be put
        # into the captions of the rest. The first link obtained wins; later link-producing
        # destinations then simply upload in parallel with everyone else.
        link_caption = ""
        parallel: dict[str, BaseDestination] = {}
        for name, destination in self.destinations.items():
            if destination.produces_link and not link_caption:
                logger.info(f"--- Processing '{name}' first (its link goes into the other captions) ---")
                result = self._deliver(name, destination, archive_path, base_caption, "")
                if not result.ok:
                    failed.append(name)
                elif result.link:
                    link_caption = result.link
            else:
                parallel[name] = destination

        if parallel:
            logger.info(f"--- Processing remaining destinations in parallel: {list(parallel.keys())} ---")
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(parallel)) as executor:
                futures = {
                    executor.submit(self._deliver, name, destination, archive_path, base_caption, link_caption): name
                    for name, destination in parallel.items()
                }
                for future in concurrent.futures.as_completed(futures):
                    if not future.result().ok:
                        failed.append(futures[future])

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
        url = self.config.monitoring.healthcheck_url
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
