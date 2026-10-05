from types import SimpleNamespace

import dispatch
import main
import store
import subscriptions
from tests.test_dispatch import FakeBot, house


def fake_module(per_city):
    """A search source: per_city maps area slug -> {id: listing}."""
    return SimpleNamespace(NAME="funda", fetch_search=lambda search: dict(per_city[search["area"]]))


def run(module, subs, bot):
    dispatch.PACER = dispatch.Pacer(0)
    return main.run_search_source(module, "funda", {}, subs, bot, None)


def test_first_fetch_of_a_search_is_silent_then_new_listings_go_out(make_sub):
    make_sub(-1, cities=(("Utrecht", 10),))
    subscriptions.rebuild_searches()
    results = {"utrecht": {"1": house("funda-1")}}
    bot = FakeBot()
    run(fake_module(results), subscriptions.all_active(), bot)
    assert bot.sent == []                                   # seeded silently
    results["utrecht"]["2"] = house("funda-2")
    run(fake_module(results), subscriptions.all_active(), bot)
    assert [m["chat_id"] for m in bot.sent] == [-1]


def test_new_city_on_an_existing_source_does_not_flood(make_sub):
    make_sub(-1, cities=(("Utrecht", 10),))
    subscriptions.rebuild_searches()
    results = {"utrecht": {"1": house("funda-1")}, "zeist": {
        "7": house("funda-7", city="Zeist"), "8": house("funda-8", city="Zeist")}}
    bot = FakeBot()
    run(fake_module(results), subscriptions.all_active(), bot)    # seeds utrecht
    subscriptions.add_location(-1, "Zeist", 10)
    subscriptions.rebuild_searches()
    run(fake_module(results), subscriptions.all_active(), bot)    # zeist is unseeded
    assert bot.sent == []
    results["zeist"]["9"] = house("funda-9", city="Zeist")
    run(fake_module(results), subscriptions.all_active(), bot)
    assert len(bot.sent) == 1


def test_each_unique_search_is_fetched_once_for_many_groups(make_sub):
    make_sub(-1, cities=(("Utrecht", 10),))
    make_sub(-2, cities=(("Utrecht", 10),))
    subscriptions.rebuild_searches()
    calls = []
    module = SimpleNamespace(NAME="funda", fetch_search=lambda s: calls.append(s["area"]) or {})
    run(module, subscriptions.all_active(), FakeBot())
    assert calls == ["utrecht"]


def test_ikwilhuren_fans_out_by_each_groups_filters(make_sub, monkeypatch):
    make_sub(-1, cities=(("Utrecht", None),))
    make_sub(-2, cities=(("Utrecht", None),), price_max=1000)
    store.record("old", notified=True, source="ikwilhuren")          # not a first run
    fresh = house("ikw-1", source="ikwilhuren")
    monkeypatch.setattr(main.ikwilhuren, "fetch_catalogue", lambda: {"old": {}, "ikw-1": fresh})
    bot = FakeBot()
    dispatch.PACER = dispatch.Pacer(0)
    result = main.run_ikwilhuren({}, subscriptions.all_active(), bot, None)
    assert [m["chat_id"] for m in bot.sent] == [-1]
    assert result["new"] == 1 and result["sent"] == 1


def test_run_cycle_skips_sources_nobody_wants(make_sub):
    make_sub(-1, cities=(("Utrecht", None),))
    subscriptions.set_source(-1, "funda", False)
    config = {"funda": {"enabled": True}}
    results = main.run_cycle(config, FakeBot(), None)
    assert results["funda"] == {"skipped": "off"}


def test_h2s_looks_up_only_cities_some_group_watches(make_sub, monkeypatch):
    make_sub(-1, cities=(("Utrecht", None),))
    store.record("old-1", city="Utrecht", notified=True, source="h2s")
    monkeypatch.setattr(main.h2s, "fetch_listing_keys", lambda: {"old-1", "utrecht-a-1", "zeist-b-2"})
    monkeypatch.setattr(main.h2s, "street_prefix", lambda key: key.rsplit("-", 1)[0])
    fetched = []

    def fetch_listing(key):
        fetched.append(key)
        return house(key, source="h2s", city="Utrecht")

    monkeypatch.setattr(main.h2s, "fetch_listing", fetch_listing)
    store.learn_street("zeist-b", "Zeist")                            # known street, unwatched city
    bot = FakeBot()
    dispatch.PACER = dispatch.Pacer(0)
    main.run_h2s({}, subscriptions.all_active(), bot, None)
    assert fetched == ["utrecht-a-1"] and len(bot.sent) == 1
