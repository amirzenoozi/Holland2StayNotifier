import migration
import store
import subscriptions

CONFIG = {
    "telegram": {"chat_id": -1001234567890, "topic_id": 184, "admin_ids": [1], "timezone": "Europe/Amsterdam"},
    "holland2stay": {"enabled": True, "cities": ["Utrecht", "Zeist"]},
    "funda": {"enabled": True, "searches": [
        {"name": "A", "area": "amersfoort", "radius": "10km", "price": "1000-2000"},
        {"name": "H", "area": "hilversum", "radius": "5km", "price": "1000-2000"}]},
    "huurwoningen": {"enabled": False, "searches": []},
    "pararius": {"enabled": False, "searches": [{"area": "zwolle", "radius": "20km", "price": "0-2000"}]},
    "ikwilhuren": {"enabled": True, "areas": [{"place": "Nijkerk", "radius_km": 40}], "cities": ["Nijmegen"]},
}


def test_migrates_the_owner_group_once():
    assert migration.migrate_from_config(CONFIG) is True
    sub = subscriptions.get_active(-1001234567890)
    assert sub.topic_id == 184 and sub.timezone == "Europe/Amsterdam"
    assert sub.sources == {"holland2stay", "funda", "ikwilhuren"}          # disabled sources off
    assert (sub.price_min, sub.price_max) == (1000, 2000)                  # enabled funda only
    keys = {(l.city, l.radius_km, l.source) for l in sub.locations}
    assert ("Utrecht", None, "holland2stay") in keys
    assert ("Amersfoort", 10.0, "funda") in keys
    assert ("Nijkerk", 40.0, "ikwilhuren") in keys
    assert ("Nijmegen", None, "ikwilhuren") in keys
    assert migration.migrate_from_config(CONFIG) is False                  # idempotent


def test_funda_still_fetches_only_its_own_two_searches():
    migration.migrate_from_config(CONFIG)
    assert sorted(s.city for s in subscriptions.searches_for("funda")) == ["amersfoort", "hilversum"]
    assert subscriptions.searches_for("ikwilhuren") == []


def test_carries_over_legacy_settings():
    store.set_setting("paused", "1")
    store.set_setting("source:funda", "0")
    store.set_setting("timezone", "Asia/Tehran")
    migration.migrate_from_config(CONFIG)
    sub = subscriptions.get(-1001234567890)
    assert sub.paused and "funda" not in sub.sources and sub.timezone == "Asia/Tehran"


def test_existing_history_means_searches_start_seeded():
    store.record("funda-1", city="Amersfoort", notified=True, source="funda")
    migration.migrate_from_config(CONFIG)
    assert all(s.seeded for s in subscriptions.searches_for("funda"))


def test_no_chat_id_means_nothing_to_migrate():
    assert migration.migrate_from_config({"telegram": {}}) is False
