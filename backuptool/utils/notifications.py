import logging
import os
import requests

logger = logging.getLogger(__name__)

def _send_to_telegram(message: str, token: str, chat_id: str):
    payload = {
        'chat_id': chat_id,
        'text': f"‼️ *CRITICAL: Backup Script Failure*\n\n```\n{message}\n```",
        'parse_mode': 'Markdown'
    }
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(api_url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("Successfully sent failure notification to Telegram.")
    except Exception as e:
        logger.error(f"Failed to send failure notification to Telegram: {e}")

def send_fatal_alert(message: str, config: dict | None = None):
    """
    Sends a fatal error alert to configured and enabled notification services.
    If config is not provided, it falls back to environment variables only.
    """
    logger.info("A fatal error was caught. Attempting to send notifications...")
    
    tg_token = os.getenv("TELEGRAM_TOKEN")
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    should_send_telegram = tg_token and tg_chat_id and \
        (config is None or config.get('services', {}).get('telegram', {}).get('enable', False))
    
    if should_send_telegram:
        _send_to_telegram(message, tg_token, tg_chat_id)