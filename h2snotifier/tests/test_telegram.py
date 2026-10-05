import json
from types import SimpleNamespace

import telegram


def fake_post(result, ok=True):
    def post(url, data=None, timeout=None):
        post.last = (url, data)
        return SimpleNamespace(ok=ok, status_code=200 if ok else 400, text="", json=lambda: result)
    return post


def test_get_chat_member_returns_status(monkeypatch):
    monkeypatch.setattr(telegram.requests, "post", fake_post({"result": {"status": "administrator"}}))
    bot = telegram.TelegramBot("T")
    assert bot.get_chat_member(-1, 5) == "administrator"


def test_get_chat_member_none_on_failure(monkeypatch):
    monkeypatch.setattr(telegram.requests, "post", fake_post({}, ok=False))
    assert telegram.TelegramBot("T").get_chat_member(-1, 5) is None


def test_get_updates_asks_for_membership_changes(monkeypatch):
    post = fake_post({"result": []})
    monkeypatch.setattr(telegram.requests, "post", post)
    telegram.TelegramBot("T").get_updates(timeout=0)
    assert "my_chat_member" in json.loads(post.last[1]["allowed_updates"])
