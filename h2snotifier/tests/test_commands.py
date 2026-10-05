import pytest

import commands
import subscriptions


@pytest.fixture(autouse=True)
def known_towns(monkeypatch):
    towns = {"utrecht", "zeist", "hilversum", "capelle aan den ijssel", "amersfoort", "nijmegen"}
    monkeypatch.setattr(commands.geo, "locate_town",
                        lambda name: (52.0, 5.0) if name.lower() in towns else None)


def test_city_add_with_radius(make_sub):
    make_sub(-1, cities=())
    assert "Utrecht" in commands.city(-1, "add Utrecht 10")
    assert [(l.city, l.radius_km) for l in subscriptions.get(-1).locations] == [("Utrecht", 10.0)]


def test_city_add_multi_word_name_without_radius(make_sub):
    make_sub(-1, cities=())
    commands.city(-1, "add Capelle aan den IJssel")
    assert subscriptions.get(-1).locations[0].city == "Capelle aan den IJssel"
    assert subscriptions.get(-1).locations[0].radius_km is None


def test_city_add_rejects_unknown_town(make_sub):
    make_sub(-1, cities=())
    assert "could not find" in commands.city(-1, "add Atlantis 10")
    assert subscriptions.get(-1).locations == []


def test_city_add_reports_limit(monkeypatch, make_sub):
    monkeypatch.setattr(subscriptions, "MAX_CITIES", 1)
    make_sub(-1, cities=())
    commands.city(-1, "add Utrecht")
    assert "at most 1" in commands.city(-1, "add Zeist")


def test_city_remove_and_list(make_sub):
    make_sub(-1, cities=(("Utrecht", None),))
    assert "Utrecht" in commands.city(-1, "")
    assert "Removed" in commands.city(-1, "remove utrecht")
    assert "not on your list" in commands.city(-1, "remove utrecht")


def test_price_set_and_clear(make_sub):
    make_sub(-1)
    commands.price(-1, "800 1800")
    assert (subscriptions.get(-1).price_min, subscriptions.get(-1).price_max) == (800, 1800)
    commands.price(-1, "- 1500")
    assert (subscriptions.get(-1).price_min, subscriptions.get(-1).price_max) == (None, 1500)
    commands.price(-1, "clear")
    assert subscriptions.get(-1).price_max is None


@pytest.mark.parametrize("bad", ["", "800", "abc 100", "1800 800", "-5 100"])
def test_price_rejects_bad_input(make_sub, bad):
    make_sub(-1)
    assert commands.price(-1, bad).startswith(("❓", "Usage"))
    assert subscriptions.get(-1).price_min is None


def test_filters_text_lists_everything(make_sub):
    make_sub(-1, cities=(("Utrecht", 10),), price_min=800, price_max=1800)
    subscriptions.set_source(-1, "funda", False)
    text = commands.filters_text(subscriptions.get(-1))
    assert "Utrecht" in text and "10 km" in text and "€800" in text and "€1800" in text
    assert "Pararius" in text and "Funda" not in text.split("Sources")[1]


def test_filters_text_for_empty_group_says_nothing_will_arrive(make_sub):
    make_sub(-1, cities=())
    assert "no alerts" in commands.filters_text(subscriptions.get(-1)).lower()
