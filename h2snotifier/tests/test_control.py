from types import SimpleNamespace

import pytest

import commands
import control
import subscriptions

OWNER = 1
CONFIG = {"telegram": {"admin_ids": [OWNER]}, "funda": {"enabled": True}, "ikwilhuren": {"enabled": True}}


class Bot:
    def __init__(self, roles=None):
        self.roles, self.sent, self.edits, self.answers, self.deleted = roles or {}, [], [], [], []

    def send_simple_msg(self, text, chat_id=None, message_thread_id=None, keyboard=None, **kw):
        self.sent.append({"chat_id": chat_id, "thread": message_thread_id, "text": text, "kb": keyboard})
        n = len(self.sent)
        return SimpleNamespace(ok=True, json=lambda: {"result": {"message_id": n}})

    def get_chat_member(self, chat_id, user_id):
        return self.roles.get((chat_id, user_id))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def edit_text(self, chat_id, message_id, text, keyboard=None):
        self.edits.append((chat_id, message_id, text))

    def answer_callback(self, callback_id, text=None, alert=False):
        self.answers.append((text, alert))

    def set_commands(self, commands_):
        pass


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, reply_to=None, force=False):
        self.calls.append(force)
        return True


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr(commands.geo, "locate_town", lambda name: (52.0, 5.0))

    def build(roles=None):
        bot, runner = Bot(roles), Runner()
        clock = SimpleNamespace(now=0.0)
        ctl = control.Controller(bot, CONFIG, runner, clock=lambda: clock.now)
        ctl.clock_state = clock
        return ctl, bot, runner

    return build


def group_msg(text, chat_id=-100, user=5, thread=None, topic=False, **extra):
    msg = {"text": text, "chat": {"id": chat_id, "type": "supergroup", "title": "Friends"},
           "from": {"id": user}, "message_id": 1}
    if thread is not None:
        msg["message_thread_id"] = thread
    if topic:
        msg["is_topic_message"] = True
    msg.update(extra)
    return {"update_id": 1, "message": msg}


def private_msg(text, user):
    return {"update_id": 1, "message": {"text": text, "chat": {"id": user, "type": "private"}, "from": {"id": user}}}


def test_owner_creates_invite_and_group_admin_redeems_it(make):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    ctl.handle(private_msg("/invite", OWNER))
    code = bot.sent[-1]["text"].split("/setup ")[1].split()[0]
    ctl.handle(group_msg(f"/setup {code}", thread=42, topic=True))
    sub = subscriptions.get_active(-100)
    assert sub is not None and sub.topic_id == 42 and sub.title == "Friends"
    assert "this topic" in bot.sent[-1]["text"]


def test_setup_in_a_plain_group_reply_does_not_pick_a_topic(make):
    ctl, bot, _ = make({(-100, 5): "creator"})
    code = subscriptions.create_invite()
    # Non-forum groups put message_thread_id on replies, but is_topic_message is absent.
    ctl.handle(group_msg(f"/setup {code}", thread=999, topic=False))
    assert subscriptions.get_active(-100).topic_id is None


def test_setup_needs_a_group_admin(make):
    ctl, bot, _ = make({(-100, 5): "member"})
    code = subscriptions.create_invite()
    ctl.handle(group_msg(f"/setup {code}"))
    assert subscriptions.get_active(-100) is None
    assert bot.sent[-1]["text"] == control.DENIED


def test_used_or_bad_invite_is_refused(make):
    ctl, bot, _ = make({(-100, 5): "administrator", (-200, 5): "administrator"})
    code = subscriptions.create_invite()
    ctl.handle(group_msg(f"/setup {code}", chat_id=-100))
    ctl.handle(group_msg(f"/setup {code}", chat_id=-200))
    assert subscriptions.get_active(-200) is None
    assert "not valid" in bot.sent[-1]["text"]


def test_commands_in_unregistered_groups_are_ignored(make):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    ctl.handle(group_msg("/city add Utrecht"))
    assert bot.sent == []


def test_non_admin_cannot_change_filters(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "member"})
    make_sub(-100, cities=())
    ctl.handle(group_msg("/city add Utrecht"))
    assert subscriptions.get(-100).locations == []
    assert bot.sent[-1]["text"] == control.DENIED


def test_admin_changes_filters_and_owner_is_always_admin(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    make_sub(-100, cities=())
    ctl.handle(group_msg("/city add Utrecht 10"))
    ctl.handle(group_msg("/price 800 1800", user=OWNER))
    sub = subscriptions.get(-100)
    assert [(l.city, l.radius_km) for l in sub.locations] == [("Utrecht", 10.0)]
    assert (sub.price_min, sub.price_max) == (800, 1800)


def test_replies_go_to_the_topic_only_when_it_is_a_topic(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    make_sub(-100)
    ctl.handle(group_msg("/filters", thread=7, topic=True))
    ctl.handle(group_msg("/filters", thread=555, topic=False))
    assert [m["thread"] for m in bot.sent] == [7, None]


def test_settopic_moves_alerts(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    make_sub(-100)
    ctl.handle(group_msg("/settopic", thread=7, topic=True))
    assert subscriptions.get(-100).topic_id == 7
    ctl.handle(group_msg("/settopic"))
    assert subscriptions.get(-100).topic_id is None


def test_stranger_in_private_chat_gets_the_invite_only_notice(make):
    ctl, bot, _ = make()
    ctl.handle(private_msg("/invite", 99))
    assert bot.sent[-1]["text"] == control.INVITE_ONLY
    assert subscriptions.has_any() is False


def test_owner_can_list_and_revoke_groups(make, make_sub):
    ctl, bot, _ = make()
    make_sub(-100)
    ctl.handle(private_msg("/groups", OWNER))
    assert "-100" in bot.sent[-1]["text"]
    ctl.handle(private_msg("/revoke -100", OWNER))
    assert subscriptions.get_active(-100) is None


def test_bot_removed_from_group_deactivates_it(make, make_sub):
    ctl, bot, _ = make()
    make_sub(-100)
    ctl.handle({"update_id": 1, "my_chat_member": {
        "chat": {"id": -100, "type": "supergroup"}, "new_chat_member": {"status": "kicked"}}})
    assert subscriptions.get_active(-100) is None


def test_supergroup_upgrade_message_moves_the_subscription(make, make_sub):
    ctl, bot, _ = make()
    make_sub(-100)
    ctl.handle({"update_id": 1, "message": {
        "chat": {"id": -100, "type": "group"}, "migrate_to_chat_id": -1001, "message_id": 2}})
    assert subscriptions.get_active(-1001) is not None and subscriptions.get(-100) is None


def test_check_cooldown_for_non_owners_and_force_for_owner(make, make_sub):
    ctl, bot, runner = make({(-100, 5): "administrator"})
    make_sub(-100)
    ctl.handle(group_msg("/check"))
    ctl.handle(group_msg("/check"))                      # inside the cooldown
    assert runner.calls == [False]
    assert "Try again" in bot.sent[-1]["text"]
    ctl.handle(group_msg("/check", user=OWNER))
    ctl.handle(group_msg("/check", user=OWNER))          # owner is never throttled
    assert runner.calls == [False, True, True]
    ctl.clock_state.now += control.CHECK_COOLDOWN_MINUTES * 60 + 1
    ctl.handle(group_msg("/check"))
    assert runner.calls[-1] is False and len(runner.calls) == 4


def test_panel_buttons_toggle_pause_and_sources_for_that_group_only(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    make_sub(-100)
    make_sub(-200)

    def tap(data, chat_id=-100):
        ctl.handle({"update_id": 1, "callback_query": {
            "id": "q", "data": data, "from": {"id": 5},
            "message": {"chat": {"id": chat_id}, "message_id": 10}}})

    tap("toggle:funda")
    tap("pause")
    assert "funda" not in subscriptions.get(-100).sources and subscriptions.get(-100).paused
    assert "funda" in subscriptions.get(-200).sources and not subscriptions.get(-200).paused
    assert bot.edits[-1][0] == -100


def test_button_from_a_non_admin_is_refused(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "member"})
    make_sub(-100)
    ctl.handle({"update_id": 1, "callback_query": {
        "id": "q", "data": "pause", "from": {"id": 5}, "message": {"chat": {"id": -100}, "message_id": 10}}})
    assert not subscriptions.get(-100).paused
    assert bot.answers[-1] == (control.DENIED, True)


def test_timezone_is_per_group(make, make_sub):
    ctl, bot, _ = make({(-100, 5): "administrator"})
    make_sub(-100)
    make_sub(-200)
    ctl.handle(group_msg("/timezone Asia/Tehran"))
    assert subscriptions.get(-100).timezone == "Asia/Tehran"
    assert subscriptions.get(-200).timezone == "Europe/Amsterdam"
