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

import logging

import registry
import store

log = logging.getLogger("control")

DENIED = "⛔ You do not have access to this feature, call AmirKhan!"

PAUSED_KEY = "paused"
SOURCE_KEY = "source:{}"

COMMANDS = (
    ("panel", "Open the control panel"),
    ("status", "What the notifier is doing"),
    ("pause", "Stop all alerts"),
    ("resume", "Start alerts again"),
    ("check", "Run a check right now"),
    ("help", "List the commands"),
)

HELP = (
    "🎛 Rental notifier\n\n"
    "/panel - buttons to pause and pick sources\n"
    "/status - what is on and how much is tracked\n"
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
            {"text": "🔄 Refresh", "callback_data": "refresh"},
        ]
    )
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


class Controller:
    """
    Long-polls Telegram and applies whatever the admin asks for.

    `run_now` is handed in rather than imported so this module never has to
    know how a scrape cycle works.
    """

    def __init__(self, bot, config, run_now):
        self.bot = bot
        self.config = config
        self.run_now = run_now
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
                if stop is not None and stop.wait(5):
                    return
                continue
            for update in updates:
                self.offset = update["update_id"] + 1
                try:
                    self.handle(update)
                except Exception:  # a bad update must not end the listener
                    log.exception("failed to handle update")

    def handle(self, update):
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

        if not is_admin(user_id, self.config):
            log.warning("denied /%s from %s", command, user_id)
            return self._reply(chat_id, thread, DENIED)

        if command in ("start", "help"):
            return self._reply(chat_id, thread, HELP)
        if command in ("panel", "sources", "control"):
            return self._reply(
                chat_id, thread, panel_text(self.config), panel_keyboard(self.config)
            )
        if command == "status":
            return self._reply(chat_id, thread, status_text(self.config))
        if command == "pause":
            set_paused(True)
            return self._reply(chat_id, thread, "⏸ Alerts paused. Nothing will be sent.")
        if command in ("resume", "start_alerts"):
            set_paused(False)
            return self._reply(chat_id, thread, "▶️ Alerts resumed.")
        if command in ("check", "run"):
            started = self.run_now()
            return self._reply(
                chat_id,
                thread,
                "⚡ Checking now…" if started else "⏳ A check is already running.",
            )
        return self._reply(chat_id, thread, HELP)

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
            note = "Checking now…" if self.run_now() else "Already running"

        self.bot.answer_callback(query["id"], note)
        if chat_id and message_id:
            self.bot.edit_text(
                chat_id, message_id, panel_text(self.config), panel_keyboard(self.config)
            )

    # -- helpers ----------------------------------------------------------

    def _reply(self, chat_id, thread, text, keyboard=None):
        return self.bot.send_simple_msg(
            text, chat_id=chat_id, message_thread_id=thread, keyboard=keyboard
        )
