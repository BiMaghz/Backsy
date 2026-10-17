import re
import requests
from pathlib import Path
from backuptool.destinations.base import BaseDestination
from backuptool.utils.helpers import retry, split_file, cleanup_files

class TelegramDestination(BaseDestination):
    def __init__(self, config: dict, killer=None):
        super().__init__('Telegram', config, killer)
        self.token = self.config['token']
        self.chat_id = self.config['chat_id']
        self.topic_id = self.config.get('topic_id')
        self.max_size_mb = 50

    @retry(max_retries=3, delay=5, backoff=2)
    def _call_api(self, method: str, data: dict, file_path: Path | None = None) -> None:
        """One Telegram API call with retries for transient errors only (a retry never re-sends earlier parts)."""
        url = f"https://api.telegram.org/bot{self.token}/{method}"

        def post(payload: dict) -> requests.Response:
            if file_path is None:
                return requests.post(url, json=payload, timeout=(10, 30))
            with open(file_path, 'rb') as f:
                return requests.post(url, data=payload, files={'document': (file_path.name, f)}, timeout=(10, 300))

        response = post(data)
        if response.status_code == 400 and data.get('parse_mode') and 'parse entities' in response.text.lower():
            # Names/URLs can contain characters that break legacy Markdown; delivering the
            # backup matters more than the formatting.
            self.logger.warning("Telegram could not parse the Markdown text; resending as plain text.")
            plain = {k: (re.sub(r'[*`]', '', v) if k in ('caption', 'text') else v)
                     for k, v in data.items() if k != 'parse_mode'}
            response = post(plain)
        response.raise_for_status()

    def send(self, archive_path: Path, base_caption: str, cloudflare_info: str = "") -> bool:
        self.logger.info(f"Processing backup for {self.name}...")

        should_send_file = self.config.get('send_file', True)
        if not should_send_file:
            self.logger.info("Configuration set to 'Notification Only'. Skipping file upload.")
            data = {
                'chat_id': self.chat_id,
                'text': base_caption + cloudflare_info,
                'parse_mode': 'Markdown',
                'disable_web_page_preview': True
            }
            if self.topic_id:
                data['message_thread_id'] = self.topic_id

            try:
                self._call_api('sendMessage', data)
                self.logger.info("Notification sent successfully.")
                return True
            except requests.exceptions.RequestException as e:
                self.logger.error(f"Error sending notification: {e}")
                raise

        file_size_mb = archive_path.stat().st_size / (1024 * 1024)
        files_to_send = []
        is_split = False

        if file_size_mb > self.max_size_mb:
            self.logger.warning(f"File is too large ({file_size_mb:.2f} MB). Splitting into chunks...")
            chunk_size = self.max_size_mb - 1
            files_to_send = split_file(archive_path, chunk_size)
            if not files_to_send: return False
            is_split = True
        else:
            files_to_send.append(archive_path)

        try:
            total_parts = len(files_to_send)
            for i, part_path in enumerate(files_to_send):

                if self.killer and self.killer.kill_now:
                    self.logger.warning("Upload interrupted by signal. Aborting Telegram upload.")
                    return False

                part_num = i + 1

                if part_num == 1:
                    part_caption = base_caption + cloudflare_info
                    if is_split:
                        part_caption += f"\n\n*[Part {part_num}/{total_parts}]*"
                else:
                    part_caption = f"*Archive:* `{archive_path.name}`\n*[Part {part_num}/{total_parts}]*"

                self.logger.info(f"Sending part {part_num}/{total_parts} to {self.name}...")
                data = {'chat_id': self.chat_id, 'caption': part_caption, 'parse_mode': 'Markdown'}
                if self.topic_id:
                    data['message_thread_id'] = self.topic_id
                self._call_api('sendDocument', data, file_path=part_path)

            self.logger.info(f"Successfully sent all parts to {self.name}.")
            return True
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error sending to {self.name}: {e}")
            raise
        finally:
            if is_split:
                cleanup_files(files_to_send)
