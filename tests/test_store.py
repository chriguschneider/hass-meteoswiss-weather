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


def test_a_newer_run_updates_the_hours_it_carries_and_re_stamps() -> None:
    """A newer run's values overwrite the hours it covers and re-stamp the run."""
    store = ForecastStore()
    _put(store, {_H0: 3000.0, _H0 + timedelta(hours=1): 3010.0})
    later = _RUN + timedelta(hours=3)
    assert _put(store, {_H0: 2900.0, _H0 + timedelta(hours=1): 2910.0},
                run=later, source="hourly")
    assert store.value_at(_P, _H0) == 2900.0
    assert store.value_at(_P, _H0 + timedelta(hours=1)) == 2910.0
    series = store.get(_P)
    assert series is not None
    assert series.provenance.run == later
    assert series.provenance.source == "hourly"


def test_a_newer_partial_run_keeps_the_previous_run_far_hours() -> None:
    """A newer run's near-only window keeps the previous run's far hours (#143).

    The eager hourly refresh fetches the near window on every changed run but the
    far remainder only at the far cadence. A near-only refresh of a new run must
    not drop the far hours the previous run still covers — they are held over,
    re-stamped to the new run, until the far refresh replaces them.
    """
    store = ForecastStore()
    near, far = _H0, _H0 + timedelta(hours=80)  # far is beyond the near window
    _put(store, {near: 3000.0, far: 3500.0})
    later = _RUN + timedelta(hours=1)
    # A new run refreshes only the near hour.
    assert _put(store, {near: 2900.0}, run=later, source="hourly")
    assert store.value_at(_P, near) == 2900.0  # near updated
    assert store.value_at(_P, far) == 3500.0  # far kept, not dropped
    series = store.get(_P)
    assert series is not None
    assert series.provenance.run == later  # whole series reads as current
    assert not store.is_stale(_P, later)


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
            "confirmed": False,
            "hours": 1,
            "stale": True,
        }
    }


def test_diagnostics_includes_fetch_metadata_when_provided() -> None:
    """Level, requests, bytes and layout appear in diagnostics when given."""
    store = ForecastStore()
    store.put(
        _P,
        {_H0: 3000.0},
        run=_RUN,
        fetched_at=_NOW,
        source="daily",
        level=1,
        requests=4,
        bytes_fetched=512,
        layout="date_major",
    )
    entry = store.as_diagnostics(_RUN)[_P]
    assert entry["level"] == 1
    assert entry["requests"] == 4
    assert entry["bytes"] == 512
    assert entry["layout"] == "date_major"


def test_diagnostics_omits_none_metadata_fields() -> None:
    """Fields not passed to put() do not appear in diagnostics."""
    store = ForecastStore()
    _put(store, {_H0: 3000.0})
    entry = store.as_diagnostics(_RUN)[_P]
    assert "level" not in entry
    assert "requests" not in entry
    assert "bytes" not in entry
    assert "layout" not in entry


def test_confirm_restamps_the_kept_series_to_the_new_run() -> None:
    """A canary-confirmed run keeps the values but reads as current (#125)."""
    store = ForecastStore()
    _put(store, {_H0: 3000.0})
    later = _RUN + timedelta(hours=3)
    assert store.confirm(_P, run=later, fetched_at=later) is True
    # The values are untouched...
    assert store.value_at(_P, _H0) == 3000.0
    # ... but the series is now current for the new run, and flagged confirmed.
    assert not store.is_stale(_P, later)
    series = store.get(_P)
    assert series is not None
    assert series.provenance.run == later
    assert series.provenance.confirmed is True
    assert series.provenance.source == "daily"  # the original source is kept


def test_confirm_is_a_noop_without_a_stored_series() -> None:
    """Confirming an unknown parameter changes nothing."""
    store = ForecastStore()
    assert store.confirm(_P, run=_RUN, fetched_at=_NOW) is False
    assert store.get(_P) is None


def test_confirm_does_not_move_a_series_backwards() -> None:
    """A confirm for an older run than the stored one is ignored."""
    store = ForecastStore()
    _put(store, {_H0: 3000.0})
    assert store.confirm(_P, run=_RUN - timedelta(hours=1), fetched_at=_NOW) is False
    assert store.get(_P).provenance.run == _RUN
    # An equal run has nothing to move forward either.
    assert store.confirm(_P, run=_RUN, fetched_at=_NOW) is False


def test_a_real_fetch_clears_a_prior_confirmation() -> None:
    """A later fetch of a confirmed parameter marks it fetched, not confirmed."""
    store = ForecastStore()
    _put(store, {_H0: 3000.0})
    later = _RUN + timedelta(hours=3)
    store.confirm(_P, run=later, fetched_at=later)
    assert _put(store, {_H0: 2900.0}, run=later, source="hourly")
    series = store.get(_P)
    assert series is not None
    assert series.provenance.confirmed is False
    assert store.value_at(_P, _H0) == 2900.0


def test_confirm_carries_fetch_metadata_from_previous_provenance() -> None:
    """A confirmation carries level/requests/bytes/layout from the stored series."""
    store = ForecastStore()
    store.put(
        _P,
        {_H0: 3000.0},
        run=_RUN,
        fetched_at=_NOW,
        source="daily",
        level=1,
        requests=4,
        bytes_fetched=512,
        layout="date_major",
    )
    later = _RUN + timedelta(hours=3)
    assert store.confirm(_P, run=later, fetched_at=later) is True
    series = store.get(_P)
    assert series is not None
    prov = series.provenance
    assert prov.confirmed is True
    assert prov.level == 1
    assert prov.requests == 4
    assert prov.bytes_fetched == 512
    assert prov.layout == "date_major"


# ---------------------------------------------------------------------------
# Escalation streak (ADR-0008 section 5)
# ---------------------------------------------------------------------------


def _put_with_level(
    store: ForecastStore, values, level: int, run=_RUN
) -> bool:
    return store.put(
        _P, values, run=run, fetched_at=_NOW, source="daily", level=level
    )


def test_escalation_streak_starts_at_zero() -> None:
    store = ForecastStore()
    assert store.escalation_streak(_P) == 0


def test_escalation_streak_increments_on_level_3_or_4() -> None:
    store = ForecastStore()
    _put_with_level(store, {_H0: 1.0}, level=3)
    assert store.escalation_streak(_P) == 1
    _put_with_level(store, {_H0: 2.0}, level=4, run=_RUN + timedelta(hours=1))
    assert store.escalation_streak(_P) == 2


def test_escalation_streak_resets_on_level_below_3() -> None:
    store = ForecastStore()
    _put_with_level(store, {_H0: 1.0}, level=3)
    _put_with_level(store, {_H0: 2.0}, level=3, run=_RUN + timedelta(hours=1))
    assert store.escalation_streak(_P) == 2
    _put_with_level(store, {_H0: 3.0}, level=2, run=_RUN + timedelta(hours=2))
    assert store.escalation_streak(_P) == 0


def test_escalation_streak_not_updated_without_level() -> None:
    """A put() without a level does not change the streak."""
    store = ForecastStore()
    _put_with_level(store, {_H0: 1.0}, level=3)
    assert store.escalation_streak(_P) == 1
    _put(store, {_H0: 2.0}, run=_RUN + timedelta(hours=1))  # no level
    assert store.escalation_streak(_P) == 1


def test_escalation_streak_not_updated_by_confirm() -> None:
    """A canary confirmation is not a fetch and must not change the streak."""
    store = ForecastStore()
    _put_with_level(store, {_H0: 1.0}, level=3)
    assert store.escalation_streak(_P) == 1
    store.confirm(_P, run=_RUN + timedelta(hours=1), fetched_at=_NOW)
    # Streak unchanged — confirm is not a real fetch.
    assert store.escalation_streak(_P) == 1
