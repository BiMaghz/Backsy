import logging
import os
import requests

logger = logging.getLogger(__name__)

def _send_to_telegram(message: str, token: str, chat_id: str, topic_id: str | None = None):
    payload = {
        'chat_id': chat_id,
        'text': f"‼️ *CRITICAL: Backup Script Failure*\n\n```\n{message}\n```",
        'parse_mode': 'Markdown'
    }

    if topic_id:
         payload['message_thread_id'] = topic_id

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
    Telegram credentials are taken from the loaded config first (so alerts work
    even when the token/chat_id were written as literal values rather than
    env-var placeholders), falling back to environment variables if no config
    is available yet (e.g. the failure happened before/while loading config.yml).
    """
    logger.info("A fatal error was caught. Attempting to send notifications...")

    tg_config = (config or {}).get('services', {}).get('telegram', {}) if config else {}
    telegram_enabled = tg_config.get('enable', False) if config is not None else True

    tg_token = tg_config.get('token') or os.getenv("TELEGRAM_TOKEN")
    tg_chat_id = tg_config.get('chat_id') or os.getenv("TELEGRAM_CHAT_ID")
    tg_topic_id = tg_config.get('topic_id') or os.getenv("TELEGRAM_TOPIC_ID")

    should_send_telegram = bool(tg_token and tg_chat_id and telegram_enabled)

    if should_send_telegram:
        _send_to_telegram(message, tg_token, tg_chat_id, tg_topic_id)
    else:
        logger.warning("Fatal alert not sent: Telegram is not configured/enabled.")