import pytest

import store
import subscriptions


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """Every test gets its own empty database."""
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))
    store.init()


@pytest.fixture
def make_sub():
    """Create an active subscription. cities: [(name, radius_km|None)]."""

    def build(chat_id=-100, cities=(("Utrecht", None),), topic_id=None, **fields):
        subscriptions.create(chat_id, topic_id, "Test group")
        for name, radius in cities:
            subscriptions.add_location(chat_id, name, radius, enforce=False)
        if "price_min" in fields or "price_max" in fields:
            subscriptions.set_price(chat_id, fields.get("price_min"), fields.get("price_max"))
        if fields.get("paused"):
            subscriptions.set_paused(chat_id, True)
        return subscriptions.get(chat_id)

    return build
