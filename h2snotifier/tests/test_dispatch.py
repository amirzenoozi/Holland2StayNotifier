import json
from types import SimpleNamespace

import dispatch
import store
import subscriptions

NOW = dispatch.Pacer(0)


def reply(ok=True, status=200, text="", body=None):
    return SimpleNamespace(ok=ok, status_code=status, text=text, json=lambda: body or {})


class FakeBot:
    def __init__(self, scripted=None):
        self.sent = []
        self.scripted = scripted or {}      # chat_id -> list of replies, popped in order

    def send_simple_msg(self, msg, button=None, chat_id=None, message_thread_id=None, **kw):
        self.sent.append({"chat_id": chat_id, "thread": message_thread_id, "msg": msg})
        queue = self.scripted.get(chat_id)
        return queue.pop(0) if queue else reply()


def house(key="funda-1", city="Utrecht", **fields):
    base = {"source": "funda", "url_key": key, "url": f"https://x/{key}", "city": city,
            "postcode": "3511 AB", "address": "Street 1", "price_excl": "1.250"}
    base.update(fields)
    return base


def test_fan_out_reaches_only_matching_groups_in_their_topic(make_sub):
    make_sub(-1, cities=(("Utrecht", None),), topic_id=9)
    make_sub(-2, cities=(("Zeist", None),))
    make_sub(-3, cities=(("Utrecht", None),), price_max=1000)
    bot = FakeBot()
    sent = dispatch.fan_out(house(), subscriptions.all_active(), bot, NOW)
    assert sent == 1
    assert [(m["chat_id"], m["thread"]) for m in bot.sent] == [(-1, 9)]


def test_same_listing_is_not_sent_twice_to_a_group(make_sub):
    make_sub(-1)
    bot = FakeBot()
    subs = subscriptions.all_active()
    assert dispatch.fan_out(house(), subs, bot, NOW) == 1
    assert dispatch.fan_out(house(), subs, bot, NOW) == 0


def test_failed_send_is_retried_later(make_sub):
    make_sub(-1)
    bot = FakeBot({-1: [reply(ok=False, status=500, text="boom")]})
    subs = subscriptions.all_active()
    assert dispatch.fan_out(house(), subs, bot, NOW) == 0
    assert len(store.pending_deliveries(3)) == 1
    dispatch.retry_pending(subs, bot, NOW)
    assert store.pending_deliveries(3) == []
    assert len(bot.sent) == 2


def test_gives_up_after_max_attempts(make_sub):
    make_sub(-1)
    bot = FakeBot({-1: [reply(ok=False, status=500, text="boom")] * 10})
    subs = subscriptions.all_active()
    dispatch.fan_out(house(), subs, bot, NOW)
    for _ in range(5):
        dispatch.retry_pending(subs, bot, NOW)
    assert len(bot.sent) == dispatch.MAX_ATTEMPTS


def test_kicked_bot_deactivates_group_and_others_still_get_it(make_sub):
    make_sub(-1)
    make_sub(-2)
    bot = FakeBot({-1: [reply(ok=False, status=403, text="Forbidden: bot was kicked from the group chat")]})
    subs = subscriptions.all_active()
    assert dispatch.fan_out(house(), subs, bot, NOW) == 1
    assert subscriptions.get_active(-1) is None
    assert subscriptions.get_active(-2) is not None
    dispatch.fan_out(house("funda-2"), subs, bot, NOW)         # -1 is skipped from now on
    assert [m["chat_id"] for m in bot.sent].count(-1) == 1


def test_deleted_topic_falls_back_to_general_and_says_so(make_sub):
    make_sub(-1, topic_id=9)
    bot = FakeBot({-1: [reply(ok=False, status=400, text="Bad Request: message thread not found")]})
    subs = subscriptions.all_active()
    assert dispatch.fan_out(house(), subs, bot, NOW) == 1
    assert subscriptions.get(-1).topic_id is None
    assert [m["thread"] for m in bot.sent] == [9, None, None]   # try, notice, resend


def test_supergroup_upgrade_moves_the_subscription(make_sub):
    make_sub(-1)
    body = {"parameters": {"migrate_to_chat_id": -1001}}
    bot = FakeBot({-1: [reply(ok=False, status=400, text="group chat was upgraded to a supergroup chat", body=body)]})
    subs = subscriptions.all_active()
    assert dispatch.fan_out(house(), subs, bot, NOW) == 1
    assert subscriptions.get(-1) is None and subscriptions.get_active(-1001) is not None
    assert bot.sent[-1]["chat_id"] == -1001


def test_paused_group_is_skipped(make_sub):
    make_sub(-1, paused=True)
    assert dispatch.fan_out(house(), subscriptions.all_active(), FakeBot(), NOW) == 0
