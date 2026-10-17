import requests
import os
from pathlib import Path
from backuptool.utils.helpers import retry, split_file, cleanup_files
from backuptool.core.config import CloudflareConfig
from .base import BaseDestination, SendResult

class CloudflareDestination(BaseDestination):
    config_model = CloudflareConfig
    produces_link = True

    def __init__(self, config: CloudflareConfig | dict, killer=None):
        super().__init__('Cloudflare', config, killer)
        self.worker_url = self.config.worker_url
        self.api_token = self.config.api_token
        if not self.api_token:
            self.logger.warning("No 'api_token' configured. The worker now rejects uploads unless its API_TOKEN secret is set and sent.")
        # The worker (and KV) cap a request at 25 MiB *including* multipart framing,
        # so a full 25 MiB chunk is rejected with 413. Stay safely below it.
        self.max_size_mb = 24

    @retry(max_retries=3, delay=10, backoff=2)
    def _upload_part(self, part_path: Path) -> str:
        """Uploads one part and returns its download token. Retries never re-upload earlier parts."""
        headers = {'Authorization': f'Bearer {self.api_token}'} if self.api_token else {}
        with open(part_path, 'rb') as f:
            response = requests.post(
                f"{self.worker_url.rstrip('/')}/upload",
                files={'file': (part_path.name, f, 'application/gzip')},
                headers=headers,
                timeout=300
            )
        response.raise_for_status()

        token = response.json().get('token')
        if not token:
            # A part without a link would silently make the backup unrecoverable from this destination.
            raise RuntimeError(f"Worker accepted '{part_path.name}' but returned no download token.")
        return token

    def _send(self, archive_path: Path, base_caption: str, link_caption: str = "") -> SendResult:
        self.logger.info(f"Processing backup for {self.name}...")
        file_size_mb = os.path.getsize(archive_path) / (1024 * 1024)

        files_to_send = []
        is_split = False

        if file_size_mb > self.max_size_mb:
            self.logger.warning(f"File is too large ({file_size_mb:.2f} MB). Attempting to split into chunks...")
            files_to_send = split_file(archive_path, self.max_size_mb)
            if not files_to_send:
                self.logger.error("File splitting failed. Aborting Cloudflare upload.")
                return SendResult(False, detail="could not split the archive")
            is_split = True
        else:
            files_to_send.append(archive_path)

        download_links = []
        try:
            total_parts = len(files_to_send)
            for i, part_path in enumerate(files_to_send):

                if self.killer and self.killer.kill_now:
                    self.logger.warning("Upload interrupted by signal. Aborting Cloudflare upload.")
                    return SendResult(False, detail="interrupted by signal")

                part_num = i + 1
                self.logger.info(f"Uploading part {part_num}/{total_parts} to {self.name}...")

                token = self._upload_part(part_path)
                download_url = f"{self.worker_url.rstrip('/')}/download/{token}"

                link_text = f"• [Download Link (Part {part_num}/{total_parts})]({download_url})"
                if not is_split:
                    link_text = f"• [Download Link]({download_url})"

                download_links.append(link_text)
                self.logger.info(f"Part {part_num} uploaded. Download token: {token}")

            self.logger.info(f"Successfully uploaded all parts to {self.name}.")
            return SendResult(True, link="\n" + "\n".join(download_links))
        finally:
            if is_split:
                cleanup_files(files_to_send)
