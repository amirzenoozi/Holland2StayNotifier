"""
Minimal Telegram client.

Supports forum topics: when `message_thread_id` is set, messages land in that
topic instead of the group's General thread. Also supports inline buttons -
URL buttons for listings, callback buttons for the control panel - and long
polling, which is how the bot hears your commands.
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

    def _url(self, method):
        return f"https://api.telegram.org/bot{self.apikey}/{method}"

    def _post(self, method, payload, retries=2, timeout=30):
        """POST to the API, backing off when Telegram says we are too fast."""
        response = None
        for attempt in range(retries + 1):
            try:
                response = requests.post(self._url(method), data=payload, timeout=timeout)
            except requests.RequestException as exc:
                log.error("telegram %s failed: %s", method, exc)
                return None
            if response.ok:
                return response

            if response.status_code == 429 and attempt < retries:
                wait = response.json().get("parameters", {}).get("retry_after", 5)
                log.warning("rate limited by Telegram, sleeping %ss", wait)
                time.sleep(wait + 1)
                continue

            log.error(
                "telegram %s failed: %s %s", method, response.status_code, response.text[:200]
            )
            return response
        return response

    def send_simple_msg(self, msg, retries=2, button=None, keyboard=None,
                        chat_id=None, message_thread_id=None):
        """
        Send a message.

        `button` is a single (text, url) link; `keyboard` is a ready-made
        inline_keyboard list for the control panel. Passing chat_id lets a
        reply go back to wherever a command came from rather than the topic.
        """
        payload = {
            "chat_id": self.chat_id if chat_id is None else chat_id,
            "text": msg,
            "disable_web_page_preview": True,
        }
        thread = self.message_thread_id if chat_id is None else message_thread_id
        if thread:
            payload["message_thread_id"] = thread
        if button:
            label, target = button
            keyboard = [[{"text": label, "url": target}]]
        if keyboard:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})

        return self._post("sendMessage", payload, retries=retries)

    def edit_keyboard(self, chat_id, message_id, keyboard):
        """Redraw a control panel in place, so toggling does not spam the chat."""
        return self._post(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": json.dumps({"inline_keyboard": keyboard}),
            },
        )

    def edit_text(self, chat_id, message_id, text, keyboard=None):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        return self._post("editMessageText", payload)

    def answer_callback(self, callback_id, text=None, alert=False):
        """Every tapped button must be answered or it spins forever."""
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        if alert:
            payload["show_alert"] = "true"
        return self._post("answerCallbackQuery", payload, retries=0)

    def set_commands(self, commands):
        """Populate the / menu so the commands are discoverable."""
        return self._post(
            "setMyCommands",
            {"commands": json.dumps([{"command": c, "description": d} for c, d in commands])},
            retries=0,
        )

    def get_updates(self, offset=None, timeout=50):
        """
        Long-poll for commands and button taps.

        Returns a list of updates, or None when the call itself failed - the
        caller should treat that as "try again", not "nothing happened".
        """
        payload = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            payload["offset"] = offset
        response = self._post("getUpdates", payload, retries=0, timeout=timeout + 15)
        if response is None or not response.ok:
            return None
        return response.json().get("result", [])
