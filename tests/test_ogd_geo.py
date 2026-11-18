"""Tests for the generic nearest-by-distance helper."""

from __future__ import annotations

from dataclasses import dataclass

from custom_components.meteoswiss_weather.ogd.geo import nearest


@dataclass(frozen=True)
class _Pt:
    name: str
    lat: float
    lon: float


_ZURICH = _Pt("zurich", 47.38, 8.54)
_BERN = _Pt("bern", 46.95, 7.45)
_GENEVA = _Pt("geneva", 46.20, 6.15)


def test_nearest_orders_by_distance() -> None:
    result = nearest([_GENEVA, _ZURICH, _BERN], 46.9, 7.4)
    assert [p.name for p in result] == ["bern", "zurich", "geneva"]


def test_nearest_limit() -> None:
    result = nearest([_GENEVA, _ZURICH, _BERN], 46.9, 7.4, limit=2)
    assert [p.name for p in result] == ["bern", "zurich"]


def test_nearest_empty() -> None:
    assert nearest([], 46.9, 7.4) == []
    assert nearest([], 46.9, 7.4, limit=1) == []
