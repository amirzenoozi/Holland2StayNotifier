"""
The Telegram control panel.

One bot serves many groups. This module gives it an inbox: it long-polls for
commands and button taps and routes each one to the group it came from, where
it flips that group's switches in the database. Nothing here scrapes.

Access is two-tier. The owner (telegram.admin_ids in config) issues invite
codes in a private chat; a group admin redeems one with /setup and from then on
that group's admins manage its filters. Unregistered groups are ignored.
"""

import itertools
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone as utc_timezone
from zoneinfo import ZoneInfo, available_timezones

import commands
import registry
import store
import subscriptions

log = logging.getLogger("control")

DENIED = "⛔ Only group admins can change this."
OWNER_ONLY = "⛔ Only the bot owner can do that."
INVITE_ONLY = (
    "🔒 This bot is invite-only. Ask the owner for a code, add me to your group, "
    "then send /setup <code> there as a group admin."
)
CHECK_COOLDOWN_MINUTES = int(os.environ.get("CHECK_COOLDOWN_MINUTES", "10"))
ADMIN_CACHE_SECONDS = 60

COMMANDS = (
    ("panel", "Open the control panel"),
    ("city", "List, add or remove cities"),
    ("price", "Set the rent range"),
    ("filters", "Show this group's filters"),
    ("status", "What the notifier is doing"),
    ("last", "When each source was last checked"),
    ("timezone", "Show or set the clock used for times"),
    ("settopic", "Send alerts to this topic"),
    ("pause", "Stop alerts for this group"),
    ("resume", "Start alerts again"),
    ("check", "Run a check right now"),
    ("help", "List the commands"),
)


def admin_ids(config):
    ids = config.get("telegram", {}).get("admin_ids", [])
    return {int(value) for value in ids}


def is_owner(user_id, config):
    return user_id in admin_ids(config)


def configured_sources(config):
    """Only sources the owner has switched on globally get buttons."""
    return [key for key in registry.CONFIG_KEYS if config.get(key, {}).get("enabled", False)]


def source_on(sub, key):
    return key in sub.sources


def user_zone(sub):
    """The group's clock, falling back to UTC rather than failing a command."""
    try:
        return sub.timezone, ZoneInfo(sub.timezone)
    except Exception:
        log.warning("unknown timezone %r, showing UTC", sub.timezone)
        return "UTC", utc_timezone.utc


def panel_text(sub, config):
    state = "⏸ Alerts are PAUSED" if sub.paused else "▶️ Alerts are running"
    on = [registry.label(k) for k in configured_sources(config) if source_on(sub, k)]
    return (
        f"🎛 Notifier control\n\n{state}\n"
        f"Listening to: {', '.join(on) if on else 'nothing'}\n\n"
        "Tap a source to switch it on or off. /filters shows your cities and price."
    )


def panel_keyboard(sub, config):
    rows = []
    for key in configured_sources(config):
        mark = "✅" if source_on(sub, key) else "🚫"
        rows.append([{"text": f"{mark} {registry.label(key)}", "callback_data": f"toggle:{key}"}])
    power = (
        {"text": "▶️ Resume alerts", "callback_data": "resume"}
        if sub.paused
        else {"text": "⏸ Pause alerts", "callback_data": "pause"}
    )
    rows.append([power])
    rows.append([
        {"text": "⚡ Check now", "callback_data": "check"},
        {"text": "🕒 Last checks", "callback_data": "last"},
    ])
    rows.append([{"text": "🔄 Refresh", "callback_data": "refresh"}])
    return rows


def status_text(sub, config):
    counts = store.counts_by_source()
    lines = ["⏸ Alerts are PAUSED" if sub.paused else "▶️ Alerts are running", ""]
    for source in registry.SOURCES:
        key = source["config"]
        if not config.get(key, {}).get("enabled", False):
            lines.append(f"➖ {source['label']} - not available")
            continue
        mark = "✅" if source_on(sub, key) else "🚫"
        elapsed = store.minutes_since_run(key)
        when = "never checked" if elapsed is None else f"checked {elapsed:.0f} min ago"
        lines.append(f"{mark} {source['label']} - {counts.get(source['store'], 0)} tracked, {when}")
    return "\n".join(lines)


def suggest_timezones(fragment, limit=8):
    """Names containing the fragment, so a typo gets a hint back."""
    fragment = fragment.strip().lower()
    if len(fragment) < 2:
        return []
    return sorted(name for name in available_timezones() if fragment in name.lower())[:limit]


def _ago(minutes):
    """Elapsed minutes as something readable at a glance."""
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes:.0f} min ago"
    hours = minutes / 60
    if hours < 24:
        whole = int(hours)
        rest = int(minutes - whole * 60)
        return f"{whole}h {rest}m ago" if rest else f"{whole}h ago"
    return f"{hours / 24:.1f} days ago"


def last_checks_text(sub, config):
    """When each source last ran, on the group's clock."""
    name, zone = user_zone(sub)
    now = datetime.now(utc_timezone.utc)
    lines = [f"🕒 Last checked  ({name})", ""]
    newest = None
    for source in registry.SOURCES:
        key = source["config"]
        if not config.get(key, {}).get("enabled", False):
            lines.append(f"➖ {source['label']} - not available")
            continue
        mark = "✅" if source_on(sub, key) else "🚫"
        moment = store.last_run(key)
        if moment is None:
            lines.append(f"{mark} {source['label']} - never checked")
            continue
        newest = moment if newest is None else max(newest, moment)
        elapsed = (now - moment).total_seconds() / 60
        stamp = moment.astimezone(zone).strftime("%a %d %b, %H:%M")
        line = f"{mark} {source['label']} - {stamp}  ({_ago(elapsed)})"
        wait = config.get(key, {}).get("min_interval_minutes")
        if wait and elapsed < wait:
            due = moment.timestamp() + wait * 60
            line += f"\n     next due {datetime.fromtimestamp(due, zone).strftime('%H:%M')}"
        lines.append(line)
    if newest is not None:
        stamp = newest.astimezone(zone).strftime("%a %d %b, %H:%M")
        lines.append(f"\nLast activity: {stamp}")
    return "\n".join(lines)


def timezone_text(sub, argument=None):
    """Show the group's clock, or change it when given a name."""
    if not argument:
        name, zone = user_zone(sub)
        here = datetime.now(zone).strftime("%a %d %b, %H:%M")
        return (
            f"🕒 Times are shown in {name}\nRight now that is {here}.\n\n"
            "Change it with a zone name, for example:\n"
            "/timezone Europe/Amsterdam\n/timezone Asia/Tehran"
        )
    name = argument.strip()
    try:
        ZoneInfo(name)
    except Exception:
        hints = suggest_timezones(name)
        tail = "\n\nDid you mean:\n" + "\n".join(hints) if hints else ""
        return f"❓ I do not know the zone {name!r}.{tail}"
    subscriptions.set_timezone(sub.chat_id, name)
    here = datetime.now(ZoneInfo(name)).strftime("%a %d %b, %H:%M")
    return f"🕒 Times now shown in {name}.\nRight now that is {here}."


class Replies:
    """
    One command, one message.

    A command that speaks twice replaces its own first message rather than
    posting a second: /check says "checking now" and then turns that very
    message into the report. The replacing stops at the command boundary -
    each new command opens a fresh slot, so asking for the panel leaves the
    report from your last check exactly where it is.

    House alerts never come through here - those are the point of the bot and
    are meant to stay.
    """

    # How many slots stay replaceable. Older ones are forgotten, which only
    # means a very late report posts a new message instead of replacing one.
    KEEP = 32

    def __init__(self, bot):
        self.bot = bot
        self.slots = OrderedDict()
        self.counter = itertools.count(1)
        self.lock = threading.Lock()

    def open(self, chat_id, thread):
        """Start a slot for one command and hand back somewhere to reply."""
        with self.lock:
            token = next(self.counter)
            self.slots[token] = None
            while len(self.slots) > self.KEEP:
                self.slots.popitem(last=False)
        return (token, chat_id, thread)

    def send(self, slot, text, keyboard=None):
        token, chat_id, thread = slot
        with self.lock:
            previous = self.slots.get(token)
            response = self.bot.send_simple_msg(
                text, chat_id=chat_id, message_thread_id=thread, keyboard=keyboard
            )
            message_id = _message_id(response)
            if token in self.slots:
                self.slots[token] = message_id
            # Delete afterwards: if the send failed, the old reply is still
            # better than nothing at all.
            if previous and previous != message_id:
                self.bot.delete_message(chat_id, previous)
        return response


def _describe(update):
    """One line saying what arrived and from whom, for the log."""
    if "message" in update:
        message = update["message"]
        who = message.get("from", {}).get("id")
        return f"message from {who}: {(message.get('text') or '')[:40]!r}"
    if "callback_query" in update:
        query = update["callback_query"]
        return f"button {query.get('data')!r} from {query.get('from', {}).get('id')}"
    return f"other: {sorted(k for k in update if k != 'update_id')}"


def _message_id(response):
    try:
        return response.json()["result"]["message_id"]
    except Exception:  # a failed send has nothing to remember
        return None


class Controller:
    """
    Long-polls Telegram and routes every command to the group it came from.

    Anyone in a registered group may read; only that group's admins (or the bot
    owner) may change anything. Unregistered groups are ignored entirely, so a
    stranger who adds the bot gets silence, not a menu.
    """

    def __init__(self, bot, config, run_now, replies=None, clock=time.monotonic):
        self.bot = bot
        self.config = config
        self.run_now = run_now
        self.replies = replies or Replies(bot)
        self.clock = clock
        self.offset = None
        self.admin_cache = {}
        self.last_check = {}

    # -- plumbing ---------------------------------------------------------

    def _discard_backlog(self):
        """
        Drop taps and commands that queued up while we were down.

        Telegram holds updates for 24 hours. Replaying them on start-up would
        apply yesterday's button taps invisibly: those callbacks are far too
        old to answer, so you would get no confirmation that anything moved.
        """
        while True:
            updates = self.bot.get_updates(offset=self.offset, timeout=0)
            if not updates:
                return
            self.offset = updates[-1]["update_id"] + 1
            log.info("discarded %d update(s) queued while offline", len(updates))

    def listen_forever(self, stop=None):
        self.bot.set_commands(COMMANDS)
        self._discard_backlog()
        log.info("listening for commands")
        while stop is None or not stop.is_set():
            updates = self.bot.get_updates(offset=self.offset)
            if updates is None:  # network hiccup or a 409 from a second poller
                # Always pause. Retrying flat out would hammer the API and
                # bury the reason in a wall of identical errors.
                if stop is not None:
                    if stop.wait(5):
                        return
                else:
                    time.sleep(5)
                continue
            for update in updates:
                self.offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception:  # a bad update must not end the listener
                    log.exception("failed to handle update")

    def handle(self, update):
        log.info("update %s", _describe(update))
        if "my_chat_member" in update:
            return self._handle_membership(update["my_chat_member"])
        if "message" in update:
            return self._handle_message(update["message"])
        if "callback_query" in update:
            return self._handle_callback(update["callback_query"])
        return None

    # -- who may do what --------------------------------------------------

    def is_admin(self, chat_id, user_id, message=None):
        if is_owner(user_id, self.config):
            return True
        # An admin posting anonymously arrives as the group itself.
        if message and (message.get("sender_chat") or {}).get("id") == chat_id:
            return True
        key, now = (chat_id, user_id), self.clock()
        cached = self.admin_cache.get(key)
        if cached and now - cached[0] < ADMIN_CACHE_SECONDS:
            return cached[1]
        result = self.bot.get_chat_member(chat_id, user_id) in ("creator", "administrator")
        self.admin_cache[key] = (now, result)
        return result

    def _check_cooldown(self, chat_id, user_id):
        """Minutes left before this group may /check again; 0 means go ahead."""
        if is_owner(user_id, self.config):
            return 0
        now, last = self.clock(), self.last_check.get(chat_id)
        window = CHECK_COOLDOWN_MINUTES * 60
        if last is not None and now - last < window:
            return int((window - (now - last)) // 60) + 1
        self.last_check[chat_id] = now
        return 0

    # -- membership and migration ----------------------------------------

    def _handle_membership(self, change):
        chat = change.get("chat", {})
        status = (change.get("new_chat_member") or {}).get("status")
        if chat.get("type") in ("group", "supergroup") and status in ("left", "kicked"):
            log.info("removed from chat %s, deactivating", chat.get("id"))
            subscriptions.deactivate(chat["id"])

    # -- messages ---------------------------------------------------------

    def _handle_message(self, message):
        chat = message["chat"]
        chat_id = chat["id"]

        if message.get("migrate_to_chat_id"):
            log.info("group %s became supergroup %s", chat_id, message["migrate_to_chat_id"])
            return subscriptions.move_chat(chat_id, message["migrate_to_chat_id"])

        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        # Group commands often arrive as /pause@our_h2stay_bot.
        command = text.split()[0].lstrip("/").split("@")[0].lower()
        argument = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
        user_id = message.get("from", {}).get("id")

        if chat.get("type") == "private":
            return self._handle_private(chat_id, user_id, command, argument)

        # message_thread_id also appears on plain replies in non-forum groups,
        # where it must not be used. Only a real forum topic counts.
        thread = message.get("message_thread_id") if message.get("is_topic_message") else None

        if command == "setup":
            return self._setup(message, chat, user_id, thread, argument)

        sub = subscriptions.get_active(chat_id)
        if sub is None:
            log.info("ignoring /%s in unregistered chat %s", command, chat_id)
            return None

        slot = self.replies.open(chat_id, thread)
        if not self.is_admin(chat_id, user_id, message):
            log.warning("denied /%s from %s in %s", command, user_id, chat_id)
            return self.replies.send(slot, DENIED)
        return self._command(sub, slot, command, argument, thread, user_id)

    def _handle_private(self, chat_id, user_id, command, argument):
        slot = self.replies.open(chat_id, None)
        if not is_owner(user_id, self.config):
            return self.replies.send(slot, INVITE_ONLY)
        if command == "invite":
            code = subscriptions.create_invite()
            return self.replies.send(
                slot,
                f"🎟 Invite code: {code}\n\nIn your friend's group (they must be an admin): "
                f"add this bot, then send\n/setup {code}\nThe code works once.",
            )
        if command == "groups":
            subs = subscriptions.all_active()
            lines = [f"{s.chat_id}  {s.title or '-'}  ({len(s.locations)} places"
                     f"{', paused' if s.paused else ''})" for s in subs]
            return self.replies.send(slot, "\n".join(lines) or "No active groups.")
        if command == "revoke":
            try:
                target = int(argument)
            except ValueError:
                return self.replies.send(slot, "Usage: /revoke <chat id>")
            subscriptions.deactivate(target)
            return self.replies.send(slot, f"Switched off {target}.")
        return self.replies.send(slot, commands.HELP_OWNER)

    def _setup(self, message, chat, user_id, thread, argument):
        chat_id = chat["id"]
        slot = self.replies.open(chat_id, thread)
        if not self.is_admin(chat_id, user_id, message):
            return self.replies.send(slot, DENIED)
        if subscriptions.get_active(chat_id):
            return self.replies.send(slot, "✅ This group is already set up. Try /help.")
        code = argument.strip()
        if not code:
            return self.replies.send(slot, commands.SETUP_USAGE)
        sub = subscriptions.activate(code, chat_id, thread, chat.get("title") or "")
        if sub is None:
            return self.replies.send(slot, "❌ That invite code is not valid or was already used.")
        where = "in this topic" if thread else "in this chat"
        return self.replies.send(slot, commands.SETUP_DONE.format(where=where))

    def _command(self, sub, slot, command, argument, thread, user_id):
        chat_id = sub.chat_id
        send = lambda text, keyboard=None: self.replies.send(slot, text, keyboard)

        if command in ("start", "help"):
            return send(commands.HELP_GROUP)
        if command in ("panel", "sources", "control"):
            return send(panel_text(sub, self.config), panel_keyboard(sub, self.config))
        if command == "status":
            return send(status_text(sub, self.config))
        if command in ("last", "checks", "lastcheck"):
            return send(last_checks_text(sub, self.config))
        if command in ("timezone", "tz", "time"):
            return send(timezone_text(sub, argument or None))
        if command == "city":
            return send(commands.city(chat_id, argument))
        if command == "price":
            return send(commands.price(chat_id, argument))
        if command == "filters":
            return send(commands.filters_text(sub))
        if command == "pause":
            subscriptions.set_paused(chat_id, True)
            return send("⏸ Alerts paused for this group.")
        if command in ("resume", "start_alerts"):
            subscriptions.set_paused(chat_id, False)
            return send("▶️ Alerts resumed.")
        if command == "settopic":
            subscriptions.set_topic(chat_id, thread)
            return send("✅ Alerts will arrive in this topic." if thread
                        else "✅ Alerts will arrive in the main chat.")
        if command in ("check", "run"):
            wait = self._check_cooldown(chat_id, user_id)
            if wait:
                return send(f"⏳ Try again in about {wait} min.")
            send("⚡ Checking now…")
            if not self.run_now(reply_to=slot, force=is_owner(user_id, self.config)):
                return send("⏳ A check is already running.")
            return None
        return send(commands.HELP_GROUP)

    # -- buttons ----------------------------------------------------------

    def _handle_callback(self, query):
        data = query.get("data") or ""
        user_id = query.get("from", {}).get("id")
        message = query.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        thread = message.get("message_thread_id") if message.get("is_topic_message") else None

        sub = subscriptions.get_active(chat_id) if chat_id else None
        if sub is None:
            return self.bot.answer_callback(query["id"], "This group is not set up.", alert=True)
        if not self.is_admin(chat_id, user_id):
            log.warning("denied button %r from %s", data, user_id)
            return self.bot.answer_callback(query["id"], DENIED, alert=True)

        note = "Updated"
        if data.startswith("toggle:"):
            key = data.split(":", 1)[1]
            if key in configured_sources(self.config):
                now_on = key not in sub.sources
                subscriptions.set_source(chat_id, key, now_on)
                note = f"{registry.label(key)} {'on' if now_on else 'off'}"
        elif data == "pause":
            subscriptions.set_paused(chat_id, True)
            note = "Alerts paused"
        elif data == "resume":
            subscriptions.set_paused(chat_id, False)
            note = "Alerts resumed"
        elif data == "check":
            wait = self._check_cooldown(chat_id, user_id)
            if wait:
                note = f"Try again in about {wait} min"
            else:
                slot = self.replies.open(chat_id, thread)
                forced = is_owner(user_id, self.config)
                note = "Checking now…" if self.run_now(reply_to=slot, force=forced) else "Already running"
        elif data == "last":
            slot = self.replies.open(chat_id, thread)
            self.replies.send(slot, last_checks_text(sub, self.config))
            note = "Last checks"

        self.bot.answer_callback(query["id"], note)
        if message_id:
            fresh = subscriptions.get(chat_id)
            self.bot.edit_text(
                chat_id, message_id, panel_text(fresh, self.config), panel_keyboard(fresh, self.config)
            )
