import requests
from pathlib import Path
from backuptool.destinations.base import BaseDestination
from backuptool.utils.helpers import retry, split_file, cleanup_files

def _send_text_message(token, chat_id, caption):
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': chat_id, 'text': caption, 'parse_mode': 'Markdown'}
    response = requests.post(api_url, json=payload, timeout=30)
    response.raise_for_status()

class TelegramDestination(BaseDestination):
    def __init__(self, config: dict):
        super().__init__('Telegram', config)
        self.token = self.config['token']
        self.chat_id = self.config['chat_id']
        self.topic_id = self.config.get('topic_id')
        self.max_size_mb = 50

    @retry(max_retries=3, delay=5, backoff=2)
    def send(self, archive_path: Path, base_caption: str, cloudflare_info: str = "") -> bool:
        self.logger.info(f"Processing backup for {self.name}...")
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
                part_num = i + 1
                
                if part_num == 1:
                    part_caption = base_caption + cloudflare_info
                    if is_split:
                        part_caption += f"\n\n*[Part {part_num}/{total_parts}]*"
                else:
                    part_caption = f"*Archive:* `{archive_path.name}`\n*[Part {part_num}/{total_parts}]*"
                
                self.logger.info(f"Sending part {part_num}/{total_parts} to {self.name}...")
                api_url = f"https://api.telegram.org/bot{self.token}/sendDocument"
                with open(part_path, 'rb') as f:
                    files = {'document': (part_path.name, f)}
                    data = {'chat_id': self.chat_id, 'caption': part_caption, 'parse_mode': 'Markdown'}
                    if self.topic_id:
                        data['message_thread_id'] = self.topic_id
                    response = requests.post(api_url, data=data, files=files, timeout=300)
                    response.raise_for_status()

            self.logger.info(f"Successfully sent all parts to {self.name}.")
            return True
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error sending to {self.name}: {e}")
            raise
        finally:
            if is_split:
                cleanup_files(files_to_send)