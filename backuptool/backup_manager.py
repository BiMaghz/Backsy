import logging
import os
import requests
import concurrent.futures
import multiprocessing
from pathlib import Path
from datetime import datetime, timezone

from backuptool.targets.base import BaseTarget
from backuptool.targets.local import LocalTarget
from backuptool.targets.remote import RemoteTarget

from backuptool.destinations.base import BaseDestination
from backuptool.destinations.cloudflare import CloudflareDestination
from backuptool.destinations.telegram import TelegramDestination
from backuptool.destinations.s3 import S3Destination

from backuptool.core.crypto import encrypt_file
from backuptool.core.signals import GracefulKiller

from backuptool.utils.helpers import calculate_checksum

logger = logging.getLogger(__name__)

class BackupManager:
    def __init__(self, config: dict, killer: GracefulKiller):
        self.config = config
        self.killer = killer
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
            's3': S3Destination,
        }
        for name, config in self.services_config.items():
            if config.get('enable'):
                logger.info(f"Initializing destination: {name}")
                if name in dest_map:
                    destinations[name] = dest_map[name](config, self.killer)
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
        
        link_caption_update = ""

        if 'cloudflare' in self.destinations:
            logger.info("--- Processing Cloudflare destination sequentially ---")
            result = self.destinations['cloudflare'].send(archive_path, base_caption)
            if isinstance(result, str):
                link_caption_update = result
        
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

        encryption_pass = os.getenv("BACKUP_ENCRYPTION_PASSWORD")
        if encryption_pass:
            logger.info("🔒 Encryption is ENABLED. Archives will be encrypted with GPG.")

        cpu_count = multiprocessing.cpu_count()
        max_concurrent_backups = max(1, min(2, cpu_count // 2))
        
        logger.info(f"Processing {len(self.targets)} target(s) with {max_concurrent_backups} concurrent worker(s)...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent_backups) as executor:
            future_to_target = {executor.submit(target.execute): target for target in self.targets}
            
            for future in concurrent.futures.as_completed(future_to_target):
                if self.killer.kill_now:
                    logger.critical("Process interrupted by signal. Stopping manager loop.")
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                target = future_to_target[future]
                logger.info(f"===== Processing result for Target: {target.name} =====")
                try:
                    archive_path = future.result()
                    
                    if archive_path and archive_path.exists():
                        final_path_to_send = archive_path
                        
                        if encryption_pass:
                            encrypted_path = encrypt_file(archive_path, encryption_pass)
                            if encrypted_path:
                                try:
                                    archive_path.unlink()
                                    logger.info(f"Removed unencrypted file: {archive_path.name}")
                                    final_path_to_send = encrypted_path
                                except OSError as e:
                                    logger.warning(f"Failed to remove unencrypted file: {e}")
                            else:
                                logger.error("Encryption failed! Aborting upload for safety.")
                                try:
                                    archive_path.unlink()
                                except: pass
                                continue 

                        self._send_to_destinations(final_path_to_send, target.name)
                        
                        try:
                            final_path_to_send.unlink()
                            logger.info(f"Cleaned up temporary archive: {final_path_to_send}")
                        except OSError as e:
                            logger.warning(f"Could not clean up archive {final_path_to_send}: {e}")
                    else:
                        logger.error(f"Backup failed for target '{target.name}'. Skipping destinations.")
                except Exception as exc:
                    logger.error(f"Target '{target.name}' generated an exception during execution: {exc}", exc_info=True)
        
        logger.info("Backup process finished for all targets.")

        hc_url = self.config.get('monitoring', {}).get('healthcheck_url')
        if hc_url:
            try:
                requests.get(hc_url, timeout=10)
                logger.info("Healthcheck ping sent successfully.")
            except Exception as e:
                logger.warning(f"Failed to send healthcheck ping: {e}")