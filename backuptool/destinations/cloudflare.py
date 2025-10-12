import requests
import os
from pathlib import Path
from backuptool.utils.helpers import retry, split_file, cleanup_files
from .base import BaseDestination

class CloudflareDestination(BaseDestination):
    def __init__(self, config: dict):
        super().__init__('Cloudflare', config)
        self.worker_url = self.config['worker_url']
        self.api_token = self.config.get('api_token')
        self.max_size_mb = 25

    @retry(max_retries=3, delay=10, backoff=2)
    def send(self, archive_path: Path, base_caption: str) -> bool | str:
        self.logger.info(f"Processing backup for {self.name}...")
        file_size_mb = os.path.getsize(archive_path) / (1024 * 1024)
        
        files_to_send = []
        is_split = False

        if file_size_mb > self.max_size_mb:
            self.logger.warning(f"File is too large ({file_size_mb:.2f} MB). Attempting to split into chunks...")
            files_to_send = split_file(archive_path, 25)
            if not files_to_send:
                self.logger.error("File splitting failed. Aborting Cloudflare upload.")
                return False
            is_split = True
        else:
            files_to_send.append(archive_path)

        download_links = []
        caption_update_string = ""
        try:
            total_parts = len(files_to_send)
            for i, part_path in enumerate(files_to_send):
                part_num = i + 1
                self.logger.info(f"Uploading part {part_num}/{total_parts} to {self.name}...")

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
                    self.logger.error(f"Cloudflare upload for part {part_num} succeeded but worker did not return a token.")
                    continue
                
                download_url = f"{self.worker_url.rstrip('/')}/download/{token}"
                
                link_text = f"• [Download Link (Part {part_num}/{total_parts})]({download_url})"
                if not is_split:
                    link_text = f"• [Download Link]({download_url})"
                
                download_links.append(link_text)
                self.logger.info(f"Part {part_num} uploaded. Download token: {token}")

            if download_links:
                caption_update_string = "\n" + "\n".join(download_links)
            
            self.logger.info(f"Successfully uploaded all parts to {self.name}.")
            return caption_update_string
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error during {self.name} upload: {e}")
            raise
        finally:
            if is_split:
                cleanup_files(files_to_send)