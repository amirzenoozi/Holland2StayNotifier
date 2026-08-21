"""
Minimal Telegram client.

Supports forum topics: when `message_thread_id` is set, messages land in that
topic instead of the group's General thread. Also supports a single inline
URL button, which is how listings link out to their page.
"""

import json
import logging
import time

import requests

log = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, apikey, chat_id, message_thread_id=None):
        self.apikey = apikey
        self.chat_id = chat_id
        self.message_thread_id = message_thread_id

    def send_simple_msg(self, msg, retries=2, button=None):
        """Send a message. `button` is an optional (text, url) inline link."""
        url = f"https://api.telegram.org/bot{self.apikey}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": msg,
            "disable_web_page_preview": True,
        }
        if self.message_thread_id:
            payload["message_thread_id"] = self.message_thread_id
        if button:
            label, target = button
            payload["reply_markup"] = json.dumps(
                {"inline_keyboard": [[{"text": label, "url": target}]]}
            )

        for attempt in range(retries + 1):
            response = requests.post(url, data=payload, timeout=30)
            if response.ok:
                return response

            # Telegram asks us to back off when the group limit is hit.
            if response.status_code == 429 and attempt < retries:
                wait = response.json().get("parameters", {}).get("retry_after", 5)
                log.warning("rate limited by Telegram, sleeping %ss", wait)
                time.sleep(wait + 1)
                continue

            log.error("telegram send failed: %s %s", response.status_code, response.text[:200])
            return response
        return response
