import registry
import store
import subscriptions


def test_create_has_all_sources_on_and_no_locations():
    sub = subscriptions.create(-100, 5, "Friends", "Europe/Amsterdam")
    assert (sub.chat_id, sub.topic_id, sub.title) == (-100, 5, "Friends")
    assert sub.active and not sub.paused
    assert sub.sources == set(registry.CONFIG_KEYS)
    assert sub.locations == []


def test_invite_is_single_use():
    code = subscriptions.create_invite()
    first = subscriptions.activate(code, -1, None, "A")
    second = subscriptions.activate(code, -2, None, "B")
    assert first is not None and first.chat_id == -1
    assert second is None
    assert subscriptions.get(-2) is None


def test_unknown_invite_is_rejected():
    assert subscriptions.activate("nope", -1, None, "A") is None
    assert subscriptions.get(-1) is None


def test_setters_round_trip():
    subscriptions.create(-1, None, "A")
    subscriptions.set_topic(-1, 77)
    subscriptions.set_paused(-1, True)
    subscriptions.set_price(-1, 800, 1800)
    subscriptions.set_timezone(-1, "Asia/Tehran")
    subscriptions.set_source(-1, "funda", False)
    sub = subscriptions.get(-1)
    assert (sub.topic_id, sub.paused, sub.price_min, sub.price_max, sub.timezone) == (
        77, True, 800, 1800, "Asia/Tehran")
    assert "funda" not in sub.sources and "pararius" in sub.sources


def test_deactivate_hides_from_active_lists():
    subscriptions.create(-1, None, "A")
    subscriptions.deactivate(-1)
    assert subscriptions.get_active(-1) is None
    assert subscriptions.all_active() == []
    assert subscriptions.get(-1) is not None
    assert subscriptions.has_any()


def test_delivery_helpers():
    assert not store.delivery_exists(-1, "funda", "funda-1")
    store.record_delivery(-1, "funda", "funda-1", ok=False, payload='{"x": 1}')
    assert store.delivery_exists(-1, "funda", "funda-1")
    assert store.pending_deliveries(3) == [(-1, "funda", "funda-1", 1, '{"x": 1}')]
    store.update_delivery(-1, "funda", "funda-1", ok=True, attempts=2)
    assert store.pending_deliveries(3) == []
