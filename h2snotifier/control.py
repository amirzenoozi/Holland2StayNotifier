"""
The Telegram control panel.

The notifier used to be a one-shot script on a timer with no way to talk to
it. This module gives it an inbox: it long-polls for commands and button
taps, and writes the answers into the settings table, which the scrape loop
consults before every cycle. Nothing here scrapes - it only flips switches.

Access is deliberately blunt. Only the user ids listed in config decide
anything; everyone else gets turned away, whether they typed a command or
tapped a button someone else's panel left lying in the chat.
"""

import itertools
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone as utc_timezone
from zoneinfo import ZoneInfo, available_timezones

import registry
import store

log = logging.getLogger("control")

DENIED = "⛔ You do not have access to this feature, call AmirKhan!"

PAUSED_KEY = "paused"
SOURCE_KEY = "source:{}"
TIMEZONE_KEY = "timezone"

# Telegram never tells us where you are - see timezone_name() - so we start
# from the clock the houses are in and let you say otherwise.
DEFAULT_TIMEZONE = "Europe/Amsterdam"

COMMANDS = (
    ("panel", "Open the control panel"),
    ("status", "What the notifier is doing"),
    ("last", "When each source was last checked"),
    ("timezone", "Show or set the clock used for times"),
    ("pause", "Stop all alerts"),
    ("resume", "Start alerts again"),
    ("check", "Run a check right now"),
    ("help", "List the commands"),
)

HELP = (
    "🎛 Rental notifier\n\n"
    "/panel - buttons to pause and pick sources\n"
    "/status - what is on and how much is tracked\n"
    "/last - when each source was last checked\n"
    "/timezone - show the clock, or /timezone Europe/Amsterdam to change it\n"
    "/pause - stop all alerts\n"
    "/resume - start alerts again\n"
    "/check - run a check right now\n"
    "/help - this message"
)


def admin_ids(config):
    ids = config.get("telegram", {}).get("admin_ids", [])
    return {int(value) for value in ids}


def is_admin(user_id, config):
    return user_id in admin_ids(config)


def is_paused():
    return store.get_setting(PAUSED_KEY, "0") == "1"


def set_paused(paused):
    store.set_setting(PAUSED_KEY, "1" if paused else "0")


def source_enabled(config_key, config):
    """
    Config says how a source starts; a button tap outranks it from then on.

    A source switched off in config.json cannot be switched on from Telegram,
    because it may well have no search terms to run.
    """
    if not config.get(config_key, {}).get("enabled", False):
        return False
    return store.get_setting(SOURCE_KEY.format(config_key), "1") == "1"


def set_source(config_key, enabled):
    store.set_setting(SOURCE_KEY.format(config_key), "1" if enabled else "0")


def configured_sources(config):
    """Only sources with a config block are worth showing buttons for."""
    return [key for key in registry.CONFIG_KEYS if config.get(key, {}).get("enabled", False)]


def panel_text(config):
    state = "⏸ Alerts are PAUSED" if is_paused() else "▶️ Alerts are running"
    on = [registry.label(key) for key in configured_sources(config) if source_enabled(key, config)]
    return (
        f"🎛 Notifier control\n\n{state}\n"
        f"Listening to: {', '.join(on) if on else 'nothing'}\n\n"
        "Tap a source to switch it on or off."
    )


def panel_keyboard(config):
    rows = []
    for key in configured_sources(config):
        mark = "✅" if source_enabled(key, config) else "🚫"
        rows.append([{"text": f"{mark} {registry.label(key)}", "callback_data": f"toggle:{key}"}])

    power = (
        {"text": "▶️ Resume alerts", "callback_data": "resume"}
        if is_paused()
        else {"text": "⏸ Pause alerts", "callback_data": "pause"}
    )
    rows.append([power])
    rows.append(
        [
            {"text": "⚡ Check now", "callback_data": "check"},
            {"text": "🕒 Last checks", "callback_data": "last"},
        ]
    )
    rows.append([{"text": "🔄 Refresh", "callback_data": "refresh"}])
    return rows


def status_text(config):
    counts = store.counts_by_source()
    lines = ["⏸ Alerts are PAUSED" if is_paused() else "▶️ Alerts are running", ""]
    for source in registry.SOURCES:
        key = source["config"]
        if not config.get(key, {}).get("enabled", False):
            lines.append(f"➖ {source['label']} - not configured")
            continue
        mark = "✅" if source_enabled(key, config) else "🚫"
        tracked = counts.get(source["store"], 0)
        elapsed = store.minutes_since_run(source["store"])
        when = "never checked" if elapsed is None else f"checked {elapsed:.0f} min ago"
        lines.append(f"{mark} {source['label']} - {tracked} tracked, {when}")
    return "\n".join(lines)


def timezone_name(config=None):
    """
    The clock to print times on.

    Telegram gives us no way to discover this. An update carries the sender's
    id, name, language_code and a UTC timestamp - nothing about where they
    are. So it is a setting: config supplies the starting value and
    /timezone overrides it from then on.
    """
    stored = store.get_setting(TIMEZONE_KEY)
    if stored:
        return stored
    configured = (config or {}).get("telegram", {}).get("timezone")
    return configured or DEFAULT_TIMEZONE


def user_zone(config=None):
    """The named zone, falling back to UTC rather than failing a command."""
    name = timezone_name(config)
    try:
        return name, ZoneInfo(name)
    except Exception:
        log.warning("unknown timezone %r, showing UTC", name)
        return "UTC", utc_timezone.utc


def set_timezone(name):
    """Store a zone name, rejecting anything zoneinfo cannot resolve."""
    name = name.strip()
    try:
        ZoneInfo(name)
    except Exception:
        raise ValueError(name)
    store.set_setting(TIMEZONE_KEY, name)
    return name


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


def last_checks_text(config):
    """
    When each source last ran, on your clock.

    A source is quiet for two very different reasons - nothing new, or it
    never ran - and only the timestamp tells them apart.
    """
    name, zone = user_zone(config)
    now = datetime.now(utc_timezone.utc)
    lines = [f"🕒 Last checked  ({name})", ""]

    newest = None
    for source in registry.SOURCES:
        key = source["config"]
        if not config.get(key, {}).get("enabled", False):
            lines.append(f"➖ {source['label']} - not configured")
            continue

        mark = "✅" if source_enabled(key, config) else "🚫"
        moment = store.last_run(source["store"])
        if moment is None:
            lines.append(f"{mark} {source['label']} - never checked")
            continue

        newest = moment if newest is None else max(newest, moment)
        elapsed = (now - moment).total_seconds() / 60
        stamp = moment.astimezone(zone).strftime("%a %d %b, %H:%M")
        line = f"{mark} {source['label']} - {stamp}  ({_ago(elapsed)})"

        # A source held back on purpose looks identical to a stalled one
        # unless we say when it comes round again.
        wait = config.get(key, {}).get("min_interval_minutes")
        if wait and elapsed < wait:
            due = moment.timestamp() + wait * 60
            line += f"\n     next due {datetime.fromtimestamp(due, zone).strftime('%H:%M')}"
        lines.append(line)

    if newest is not None:
        stamp = newest.astimezone(zone).strftime("%a %d %b, %H:%M")
        lines.append(f"\nLast activity: {stamp}")
    return "\n".join(lines)


def timezone_text(config, argument=None):
    """Show the clock, or change it when given a name."""
    if not argument:
        name, zone = user_zone(config)
        here = datetime.now(zone).strftime("%a %d %b, %H:%M")
        return (
            f"🕒 Times are shown in {name}\nRight now that is {here}.\n\n"
            "Change it with a zone name, for example:\n"
            "/timezone Europe/Amsterdam\n/timezone Asia/Tehran"
        )

    try:
        name = set_timezone(argument)
    except ValueError:
        hints = suggest_timezones(argument)
        tail = "\n\nDid you mean:\n" + "\n".join(hints) if hints else ""
        return f"❓ I do not know the zone {argument!r}.{tail}"

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
    Long-polls Telegram and applies whatever the admin asks for.

    `run_now` is handed in rather than imported so this module never has to
    know how a scrape cycle works.
    """

    def __init__(self, bot, config, run_now, replies=None):
        self.bot = bot
        self.config = config
        self.run_now = run_now
        self.replies = replies or Replies(bot)
        self.offset = None

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
        # Logged so a silent bot can be told apart from one Telegram is not
        # forwarding to - privacy mode hides plain group messages from bots.
        log.info("update %s", _describe(update))
        if "message" in update:
            return self._handle_message(update["message"])
        if "callback_query" in update:
            return self._handle_callback(update["callback_query"])

    # -- commands ---------------------------------------------------------

    def _handle_message(self, message):
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        # Group commands often arrive as /pause@our_h2stay_bot.
        command = text.split()[0].lstrip("/").split("@")[0].lower()
        chat_id = message["chat"]["id"]
        thread = message.get("message_thread_id")
        user_id = message.get("from", {}).get("id")

        # This command gets its own slot. Anything it says later replaces what
        # it said before, and nothing it does touches an earlier command.
        slot = self.replies.open(chat_id, thread)

        if not is_admin(user_id, self.config):
            log.warning("denied /%s from %s", command, user_id)
            return self.replies.send(slot, DENIED)

        if command in ("start", "help"):
            return self.replies.send(slot, HELP)
        if command in ("panel", "sources", "control"):
            return self.replies.send(
                slot, panel_text(self.config), panel_keyboard(self.config)
            )
        if command == "status":
            return self.replies.send(slot, status_text(self.config))
        if command in ("last", "checks", "lastcheck"):
            return self.replies.send(slot, last_checks_text(self.config))
        if command in ("timezone", "tz", "time"):
            # Everything after the command word is the zone name.
            argument = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else None
            return self.replies.send(slot, timezone_text(self.config, argument))
        if command == "pause":
            set_paused(True)
            return self.replies.send(slot, "⏸ Alerts paused. Nothing will be sent.")
        if command in ("resume", "start_alerts"):
            set_paused(False)
            return self.replies.send(slot, "▶️ Alerts resumed.")
        if command in ("check", "run"):
            # Acknowledge before starting. A quick cycle can finish and post
            # its report first, and then the report would be the thing that
            # gets replaced - leaving you staring at "checking now" forever.
            self.replies.send(slot, "⚡ Checking all sources now…")
            # Hand the slot over so the report lands on top of that "checking
            # now" rather than beside it. A check that finds nothing must
            # still say so: silence is indistinguishable from a dead bot.
            if not self.run_now(reply_to=slot):
                return self.replies.send(slot, "⏳ A check is already running.")
            return None
        return self.replies.send(slot, HELP)

    # -- buttons ----------------------------------------------------------

    def _handle_callback(self, query):
        data = query.get("data") or ""
        user_id = query.get("from", {}).get("id")
        message = query.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        if not is_admin(user_id, self.config):
            log.warning("denied button %r from %s", data, user_id)
            return self.bot.answer_callback(query["id"], DENIED, alert=True)

        note = "Updated"
        if data.startswith("toggle:"):
            key = data.split(":", 1)[1]
            now_on = not source_enabled(key, self.config)
            set_source(key, now_on)
            note = f"{registry.label(key)} {'on' if now_on else 'off'}"
        elif data == "pause":
            set_paused(True)
            note = "Alerts paused"
        elif data == "resume":
            set_paused(False)
            note = "Alerts resumed"
        elif data == "check":
            # A tap is its own command, so the report gets a fresh slot and
            # will not swallow the panel that is sitting right above it.
            slot = self.replies.open(chat_id, message.get("message_thread_id"))
            note = "Checking now…" if self.run_now(reply_to=slot) else "Already running"
        elif data == "last":
            # Same reasoning: its own slot, posted below the untouched panel.
            slot = self.replies.open(chat_id, message.get("message_thread_id"))
            self.replies.send(slot, last_checks_text(self.config))
            note = "Last checks"

        self.bot.answer_callback(query["id"], note)
        if chat_id and message_id:
            # The panel is edited where it stands, so it stays put no matter
            # what else the bot says afterwards.
            self.bot.edit_text(
                chat_id, message_id, panel_text(self.config), panel_keyboard(self.config)
            )
