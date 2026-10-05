import pytest

import subscriptions
from subscriptions import LimitError, Search


def test_radius_bucket_rounds_up_to_supported_step():
    assert subscriptions.radius_bucket("funda", 7) == 10.0
    assert subscriptions.radius_bucket("funda", 10) == 10.0
    assert subscriptions.radius_bucket("funda", 12) == 15.0
    assert subscriptions.radius_bucket("funda", 80) == 50.0   # beyond the top step
    assert subscriptions.radius_bucket("funda", None) == 0.0
    assert subscriptions.radius_bucket("somesource", 7) == 7.0


def test_add_and_remove_location_is_case_insensitive(make_sub):
    make_sub(-1, cities=())
    subscriptions.add_location(-1, "Utrecht", 10)
    subscriptions.add_location(-1, "utrecht", 20)           # replaces, not duplicates
    assert [(l.city, l.radius_km) for l in subscriptions.get(-1).locations] == [("utrecht", 20.0)]
    assert subscriptions.remove_location(-1, "UTRECHT") is True
    assert subscriptions.remove_location(-1, "Utrecht") is False


def test_city_limit(monkeypatch, make_sub):
    monkeypatch.setattr(subscriptions, "MAX_CITIES", 2)
    make_sub(-1, cities=())
    subscriptions.add_location(-1, "Utrecht")
    subscriptions.add_location(-1, "Zeist")
    with pytest.raises(LimitError):
        subscriptions.add_location(-1, "Hilversum")
    subscriptions.add_location(-1, "Zeist", 5)               # editing an existing one is fine


def test_search_cap_counts_distinct_paid_searches(monkeypatch, make_sub):
    monkeypatch.setattr(subscriptions, "MAX_SEARCHES", 3)    # 3 paid sources = one city
    make_sub(-1, cities=())
    make_sub(-2, cities=())
    subscriptions.add_location(-1, "Utrecht", 10)            # 3 searches
    with pytest.raises(LimitError):
        subscriptions.add_location(-1, "Zeist", 10)
    subscriptions.add_location(-2, "Utrecht", 10)            # shared: adds nothing new


def test_rebuild_searches_and_seeding(make_sub):
    make_sub(-1, cities=())
    subscriptions.add_location(-1, "Utrecht", 7)
    keys = subscriptions.rebuild_searches()
    assert ("funda", "utrecht", 10.0) in keys and ("pararius", "utrecht", 10.0) in keys
    found = subscriptions.searches_for("funda")
    assert found == [Search("utrecht", 10.0, False)]
    subscriptions.mark_seeded("funda", found[0])
    assert subscriptions.searches_for("funda")[0].seeded is True
    subscriptions.rebuild_searches()                          # unchanged set keeps the flag
    assert subscriptions.searches_for("funda")[0].seeded is True
    subscriptions.remove_location(-1, "Utrecht")
    subscriptions.rebuild_searches()
    assert subscriptions.searches_for("funda") == []


def test_search_fetch_args():
    assert Search("capelle aan den ijssel", 10.0, False).fetch_args() == {
        "name": "capelle aan den ijssel 10km", "area": "capelle-aan-den-ijssel", "radius": "10km"}
    assert Search("zeist", 0.0, False).fetch_args() == {"name": "zeist", "area": "zeist"}


def test_source_specific_locations_do_not_create_paid_searches(make_sub):
    make_sub(-1, cities=())
    subscriptions.add_location(-1, "Nijkerk", 40, source="ikwilhuren", enforce=False)
    assert subscriptions.rebuild_searches() == set()
    sub = subscriptions.get(-1)
    assert sub.locations_for("funda") == []
    assert [l.city for l in sub.locations_for("ikwilhuren")] == ["Nijkerk"]


def test_wants_and_cities_for(make_sub):
    make_sub(-1, cities=(("Utrecht", None),))
    make_sub(-2, cities=(("Zeist", None),))
    subscriptions.set_source(-2, "holland2stay", False)
    subs = subscriptions.all_active()
    assert subscriptions.wants("holland2stay", subs)
    assert subscriptions.cities_for("holland2stay", subs) == {"utrecht"}
    subscriptions.set_source(-1, "holland2stay", False)
    assert not subscriptions.wants("holland2stay", subscriptions.all_active())


def test_move_chat_keeps_settings_and_reactivates(make_sub):
    make_sub(-1, cities=(("Utrecht", 10),), price_min=500)
    subscriptions.deactivate(-1)
    subscriptions.move_chat(-1, -1001)
    assert subscriptions.get(-1) is None
    moved = subscriptions.get_active(-1001)
    assert moved.price_min == 500
    assert [l.city for l in moved.locations] == ["Utrecht"]
