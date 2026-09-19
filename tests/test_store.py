"""Unit tests for the run-scoped forecast store (ADR-0008). No Home Assistant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.meteoswiss_weather.store import ForecastStore

_RUN = datetime(2026, 9, 18, 5, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 18, 5, 4, tzinfo=UTC)
_H0 = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
_P = "zprfr0hs"


def _put(store: ForecastStore, values, run=_RUN, source="daily") -> bool:
    return store.put(_P, values, run=run, fetched_at=_NOW, source=source)


def test_empty_store_has_no_values_and_everything_is_stale() -> None:
    store = ForecastStore()
    assert store.get(_P) is None
    assert store.value_at(_P, _H0) is None
    assert store.is_stale(_P, _RUN)
    assert store.as_diagnostics(_RUN) == {}


def test_put_then_read_back() -> None:
    store = ForecastStore()
    assert _put(store, {_H0: 3000.0})
    assert store.value_at(_P, _H0) == 3000.0
    assert store.value_at(_P, _H0 + timedelta(hours=1)) is None
    assert not store.is_stale(_P, _RUN)


def test_an_empty_series_never_replaces_the_last_good_one() -> None:
    """A refresh that delivered nothing keeps the previous run (section 1)."""
    store = ForecastStore()
    _put(store, {_H0: 3000.0})
    later = _RUN + timedelta(hours=1)
    assert not _put(store, {}, run=later)
    assert store.value_at(_P, _H0) == 3000.0
    # ... but the parameter is now known to be held over from an older run.
    assert store.is_stale(_P, later)


def test_an_older_run_is_ignored() -> None:
    store = ForecastStore()
    _put(store, {_H0: 3000.0})
    assert not _put(store, {_H0: 1.0}, run=_RUN - timedelta(hours=1))
    assert store.value_at(_P, _H0) == 3000.0


def test_a_newer_run_replaces_the_series() -> None:
    store = ForecastStore()
    _put(store, {_H0: 3000.0, _H0 + timedelta(hours=1): 3010.0})
    later = _RUN + timedelta(hours=3)
    assert _put(store, {_H0: 2900.0}, run=later, source="hourly")
    assert store.value_at(_P, _H0) == 2900.0
    assert store.value_at(_P, _H0 + timedelta(hours=1)) is None
    series = store.get(_P)
    assert series is not None
    assert series.provenance.run == later
    assert series.provenance.source == "hourly"


def test_the_same_run_is_merged_across_paths() -> None:
    """The daily block spans the run, the hourly fetch only its horizon."""
    store = ForecastStore()
    h1 = _H0 + timedelta(hours=1)
    _put(store, {_H0: 3000.0})
    assert _put(store, {h1: 3010.0}, source="hourly")
    assert store.value_at(_P, _H0) == 3000.0
    assert store.value_at(_P, h1) == 3010.0
    # Nothing new: the store reports no change, so nobody is re-rendered.
    assert not _put(store, {h1: 3010.0}, source="hourly")


def test_diagnostics_summary() -> None:
    store = ForecastStore()
    _put(store, {_H0: 3000.0})
    assert store.as_diagnostics(_RUN + timedelta(hours=1)) == {
        _P: {
            "run": _RUN.isoformat(),
            "fetched_at": _NOW.isoformat(),
            "source": "daily",
            "hours": 1,
            "stale": True,
        }
    }
