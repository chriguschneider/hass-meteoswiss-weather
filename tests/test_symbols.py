"""Tests for custom_components.meteoswiss_weather.symbols.

Covers: every mapped code returns a valid HA condition, day/night pairs,
and unknown codes return None.
"""

from __future__ import annotations

import pytest

from custom_components.meteoswiss_weather.symbols import (
    _DAY_CONDITIONS,
    condition_for_symbol,
)

# The complete set of HA ATTR_CONDITION_* values this integration may return.
_VALID_CONDITIONS = {
    "sunny",
    "clear-night",
    "partlycloudy",
    "cloudy",
    "fog",
    "rainy",
    "pouring",
    "snowy",
    "snowy-rainy",
    "lightning",
    "lightning-rainy",
    "hail",
    "windy",
    "exceptional",
}


# ---------------------------------------------------------------------------
# Table completeness
# ---------------------------------------------------------------------------


def test_every_day_code_returns_a_valid_condition() -> None:
    """Every entry in _DAY_CONDITIONS must yield a known HA condition."""
    for code, condition in _DAY_CONDITIONS.items():
        assert condition in _VALID_CONDITIONS, (
            f"Day code {code} maps to unknown condition {condition!r}"
        )


def test_every_mapped_day_code_round_trips() -> None:
    """condition_for_symbol returns a non-None result for every mapped day code."""
    for code in _DAY_CONDITIONS:
        result = condition_for_symbol(code)
        assert result is not None, f"Mapped day code {code} returned None"
        assert result in _VALID_CONDITIONS


def test_every_night_code_round_trips() -> None:
    """Night codes 101-142 each return a valid condition."""
    for day_code in _DAY_CONDITIONS:
        night_code = day_code + 100
        result = condition_for_symbol(night_code)
        assert result is not None, f"Night code {night_code} returned None"
        assert result in _VALID_CONDITIONS


# ---------------------------------------------------------------------------
# Day / night semantics
# ---------------------------------------------------------------------------


def test_code_1_is_sunny() -> None:
    assert condition_for_symbol(1) == "sunny"


def test_code_101_is_clear_night() -> None:
    assert condition_for_symbol(101) == "clear-night"


def test_code_2_day_is_sunny() -> None:
    # Code 2 also maps to sunny.
    assert condition_for_symbol(2) == "sunny"


def test_code_102_night_is_clear_night() -> None:
    # Night counterpart of a sunny day code → clear-night.
    assert condition_for_symbol(102) == "clear-night"


def test_code_3_day_is_partlycloudy() -> None:
    assert condition_for_symbol(3) == "partlycloudy"


def test_code_103_night_is_partlycloudy() -> None:
    # Non-sunny night code keeps the day condition.
    assert condition_for_symbol(103) == "partlycloudy"


def test_is_daytime_false_turns_sunny_into_clear_night() -> None:
    # A day code with is_daytime=False returns clear-night when the condition
    # would be sunny.
    assert condition_for_symbol(1, is_daytime=False) == "clear-night"
    assert condition_for_symbol(2, is_daytime=False) == "clear-night"


def test_is_daytime_false_does_not_affect_non_sunny_codes() -> None:
    # is_daytime=False has no effect on non-sunny conditions.
    assert condition_for_symbol(3, is_daytime=False) == "partlycloudy"
    assert condition_for_symbol(5, is_daytime=False) == "cloudy"


def test_is_daytime_true_returns_sunny_for_day_code_1() -> None:
    assert condition_for_symbol(1, is_daytime=True) == "sunny"


def test_is_daytime_none_returns_sunny_for_day_code_1() -> None:
    assert condition_for_symbol(1, is_daytime=None) == "sunny"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_none_input_returns_none() -> None:
    assert condition_for_symbol(None) is None


def test_unknown_code_returns_none() -> None:
    assert condition_for_symbol(999) is None


def test_unknown_night_code_returns_none() -> None:
    # A night code with no day base in the table returns None.
    assert condition_for_symbol(199) is None


def test_zero_returns_none() -> None:
    assert condition_for_symbol(0) is None


def test_negative_returns_none() -> None:
    assert condition_for_symbol(-1) is None


def test_unknown_code_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    """Unknown codes emit exactly one DEBUG log entry per unique code."""
    import logging

    from custom_components.meteoswiss_weather import symbols as sym_module

    unique_code = 888  # not in the table
    sym_module._unknown_logged.discard(unique_code)  # reset for this test

    logger_name = "custom_components.meteoswiss_weather.symbols"
    with caplog.at_level(logging.DEBUG, logger=logger_name):
        condition_for_symbol(unique_code)
        condition_for_symbol(unique_code)  # second call must not add another log

    messages = [r.message for r in caplog.records if str(unique_code) in r.message]
    assert len(messages) == 1, f"Expected 1 log, got {len(messages)}: {messages}"
