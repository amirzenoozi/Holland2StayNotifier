from types import SimpleNamespace

import pytest

import geo
import matcher
from subscriptions import Location, Subscription

POINTS = {"utrecht": (52.0907, 5.1214), "amersfoort": (52.1561, 5.3878)}  # ~19 km apart


@pytest.fixture(autouse=True)
def fake_geo(monkeypatch):
    monkeypatch.setattr(matcher, "geo", SimpleNamespace(
        locate_town=lambda name: POINTS.get((name or "").lower()),
        locate_listing=lambda postcode, city=None: POINTS.get((city or "").lower()),
        distance_km=geo.distance_km,
    ))


def sub(**fields):
    base = dict(chat_id=1, topic_id=None, title="", price_min=None, price_max=None,
                timezone="UTC", paused=False, active=True,
                locations=[Location("Utrecht")], sources={"funda", "ikwilhuren", "holland2stay"})
    base.update(fields)
    return Subscription(**base)


def listing(**fields):
    base = {"source": "funda", "city": "Utrecht", "postcode": "3511 AB", "price_excl": "1.250"}
    base.update(fields)
    return base


@pytest.mark.parametrize("raw,expected", [
    ("1.250", 1250), ("1,250", 1250), ("1.250,00", 1250), ("1550.00", 1550),
    ("€ 950", 950), (None, None), ("", None), ("on request", None),
])
def test_parse_price(raw, expected):
    assert matcher.parse_price(raw) == expected


def test_exact_city_matches_case_insensitively():
    assert matcher.matches(listing(city="UTRECHT"), sub())
    assert not matcher.matches(listing(city="Zeist"), sub())


def test_group_without_cities_gets_nothing():
    assert not matcher.matches(listing(), sub(locations=[]))


def test_paused_inactive_or_disabled_source_never_matches():
    assert not matcher.matches(listing(), sub(paused=True))
    assert not matcher.matches(listing(), sub(active=False))
    assert not matcher.matches(listing(), sub(sources={"ikwilhuren"}))


def test_price_bounds():
    low, high = sub(price_min=1000, price_max=1500), sub(price_max=1000)
    assert matcher.matches(listing(price_excl="1.250"), low)
    assert not matcher.matches(listing(price_excl="900"), low)
    assert not matcher.matches(listing(price_excl="1.250"), high)


def test_listing_without_price_passes_price_filter():
    assert matcher.matches(listing(price_excl=None, price_on_request=True), sub(price_min=500, price_max=900))


def test_radius_uses_distance_not_name():
    near = sub(locations=[Location("Utrecht", 25)])
    far = sub(locations=[Location("Utrecht", 10)])
    other = listing(city="Amersfoort")
    assert matcher.matches(other, near)
    assert not matcher.matches(other, far)


def test_holland2stay_matches_by_name_only():
    radius = sub(locations=[Location("Utrecht", 25)])
    assert not matcher.matches(listing(source="h2s", city="Amersfoort"), radius)
    assert matcher.matches(listing(source="h2s", city="Utrecht"), radius)


def test_source_specific_location_only_applies_to_its_source():
    s = sub(locations=[Location("Utrecht", None, "ikwilhuren")])
    assert matcher.matches(listing(source="ikwilhuren"), s)
    assert not matcher.matches(listing(source="funda"), s)


def test_unlocatable_listing_does_not_match_radius():
    s = sub(locations=[Location("Utrecht", 25)])
    assert not matcher.matches(listing(city="Nowhere"), s)
