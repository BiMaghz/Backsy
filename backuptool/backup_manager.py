import logging
import os
import concurrent.futures
from pathlib import Path
from datetime import datetime, timezone

from backuptool.targets.base import BaseTarget
from backuptool.targets.local import LocalTarget
from backuptool.targets.remote import RemoteTarget

from backuptool.destinations.base import BaseDestination
from backuptool.destinations.cloudflare import CloudflareDestination
from backuptool.destinations.telegram import TelegramDestination

from backuptool.utils.helpers import calculate_checksum

logger = logging.getLogger(__name__)

class BackupManager:
    def __init__(self, config: dict):
        self.config = config
        self.tmp_dir = Path("/tmp/Backsy") # Or your preferred temp location like /dev/shm/Backsy
        self.targets_config = config.get('targets', {})
        self.services_config = config.get('services', {})
        
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
        }
        for name, config in self.services_config.items():
            if config.get('enable'):
                logger.info(f"Initializing destination: {name}")
                if name in dest_map:
                    destinations[name] = dest_map[name](config)
        return destinations

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

    def _send_to_destinations(self, archive_path: Path, target_name: str):
        if not self.destinations:
            logger.warning("No destinations enabled. Backup archive is only available locally in temp.")
            return

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
        
        cloudflare_caption_update = ""
        if 'cloudflare' in self.destinations:
            logger.info("--- Processing Cloudflare destination sequentially ---")
            result = self.destinations['cloudflare'].send(archive_path, base_caption)
            if isinstance(result, str):
                cloudflare_caption_update = result
        
        other_destinations = {n: d for n, d in self.destinations.items() if n != 'cloudflare'}

        if other_destinations:
            logger.info(f"--- Processing remaining destinations in parallel: {list(other_destinations.keys())} ---")
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(other_destinations)) as executor:
                future_to_dest = {
                    executor.submit(dest.send, archive_path, base_caption, cloudflare_caption_update): name 
                    for name, dest in other_destinations.items()
                }
                for future in concurrent.futures.as_completed(future_to_dest):
                    dest_name = future_to_dest[future]
                    try:
                        future.result()
                    except Exception as exc:
                        logger.error(f"Parallel task for '{dest_name}' generated an exception: {exc}")

    def run(self):
        logger.info("Backup process started for all configured targets.")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        
        if not self._check_disk_space(self.tmp_dir):
            return

        if not self.targets:
            logger.warning("No targets configured in 'config.yml'. Exiting.")
            return

        logger.info(f"Processing {len(self.targets)} target(s) in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(self.targets)) as executor:
            future_to_target = {executor.submit(target.execute): target for target in self.targets}
            
            for future in concurrent.futures.as_completed(future_to_target):
                target = future_to_target[future]
                logger.info(f"===== Processing result for Target: {target.name} =====")
                try:
                    archive_path = future.result()
                    if archive_path and archive_path.exists():
                        self._send_to_destinations(archive_path, target.name)
                        try:
                            archive_path.unlink()
                            logger.info(f"Cleaned up temporary archive: {archive_path}")
                        except OSError as e:
                            logger.warning(f"Could not clean up archive {archive_path}: {e}")
                    else:
                        logger.error(f"Backup failed for target '{target.name}'. Skipping destinations.")
                except Exception as exc:
                    logger.error(f"Target '{target.name}' generated an exception during execution: {exc}", exc_info=True)
        
        logger.info("Backup process finished for all targets.")