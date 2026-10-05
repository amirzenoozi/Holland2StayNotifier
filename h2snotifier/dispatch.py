"""
Send a listing to every group it matches.

One place owns the messy part of talking to many chats: pacing per chat (a
group allows about 20 messages a minute), noticing that a group is gone or has
moved, and remembering failed sends so a later cycle can retry them.
"""

import json
import logging
import time

import render
import store
import subscriptions
from matcher import matches

log = logging.getLogger("dispatch")

MAX_ATTEMPTS = 3
PACE_SECONDS = 3.0

# Telegram's wording when the bot can no longer post in a chat.
GONE = (
    "bot was kicked", "bot was blocked", "chat not found", "bot is not a member",
    "group chat was deleted", "not enough rights to send",
)
THREAD_GONE = ("message thread not found", "topic_deleted", "topic deleted")


class Pacer:
    """Spaces messages to the same chat; different chats never wait on each other."""

    def __init__(self, seconds=PACE_SECONDS, clock=time.monotonic, sleep=time.sleep):
        self.seconds, self.clock, self.sleep = seconds, clock, sleep
        self.last = {}

    def wait(self, chat_id):
        gap = self.seconds - (self.clock() - self.last.get(chat_id, float("-inf")))
        if gap > 0:
            self.sleep(gap)
        self.last[chat_id] = self.clock()


PACER = Pacer()


def _migrated_chat(response):
    try:
        return response.json().get("parameters", {}).get("migrate_to_chat_id")
    except Exception:
        return None


def _send(bot, sub, listing, pacer):
    """One send to one group. True when delivered."""
    pacer.wait(sub.chat_id)
    response = bot.send_simple_msg(
        render.listing_to_msg(listing),
        button=render.listing_button(listing),
        chat_id=sub.chat_id,
        message_thread_id=sub.topic_id,
    )
    if getattr(response, "ok", False):
        return True

    body = (getattr(response, "text", "") or "").lower()
    status = getattr(response, "status_code", None)

    new_id = _migrated_chat(response)
    if new_id:
        log.warning("group %s became supergroup %s", sub.chat_id, new_id)
        subscriptions.move_chat(sub.chat_id, new_id)
        sub.chat_id = new_id
        return _send(bot, sub, listing, pacer)

    if sub.topic_id and any(phrase in body for phrase in THREAD_GONE):
        log.warning("group %s: topic %s is gone, falling back to General", sub.chat_id, sub.topic_id)
        subscriptions.set_topic(sub.chat_id, None)
        sub.topic_id = None
        bot.send_simple_msg(
            "⚠️ The topic alerts were going to no longer exists, so I moved them to the "
            "main chat. Send /settopic inside a topic to pick another one.",
            chat_id=sub.chat_id,
        )
        return _send(bot, sub, listing, pacer)

    if status == 403 or any(phrase in body for phrase in GONE):
        log.warning("group %s is unreachable, deactivating: %s", sub.chat_id, body[:120])
        subscriptions.deactivate(sub.chat_id)
        sub.active = False
    return False


def fan_out(listing, subs, bot, pacer=None):
    """Send `listing` to every matching group not already sent it. Returns how many."""
    pacer = pacer or PACER
    sent = 0
    for sub in subs:
        if not matches(listing, sub):
            continue
        if store.delivery_exists(sub.chat_id, listing["source"], listing["url_key"]):
            continue
        ok = _send(bot, sub, listing, pacer)
        store.record_delivery(
            sub.chat_id, listing["source"], listing["url_key"], ok,
            None if ok else json.dumps(listing),
        )
        sent += bool(ok)
    return sent


def retry_pending(subs, bot, pacer=None):
    """Give failed sends another go; abandon those whose group is gone."""
    pacer = pacer or PACER
    by_chat = {sub.chat_id: sub for sub in subs}
    for chat_id, source, url_key, attempts, payload in store.pending_deliveries(MAX_ATTEMPTS):
        sub = by_chat.get(chat_id)
        if sub is None or not sub.active:
            store.update_delivery(chat_id, source, url_key, False, MAX_ATTEMPTS)
            continue
        if sub.paused:
            continue
        ok = _send(bot, sub, json.loads(payload), pacer)
        store.update_delivery(chat_id, source, url_key, ok, attempts + 1)
