"""Tests for custom_components.meteoswiss_weather.symbols.

These pin a set of codes to the condition the *reference* icon set assigns
(``CODE_TO_CONDITION_MAP`` in Rudd-O/homeassistant-meteoswiss, dumped from the
official MeteoSwiss weather-icon spreadsheet).  They are deliberately literal:
a future rewrite of the table cannot silently pass by validating the table
against itself (the failure shape of #34 and #44).
"""

from __future__ import annotations

import pytest

from custom_components.meteoswiss_weather.symbols import (
    _CONDITIONS,
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

# Codes pinned to their expected condition, derived from the reference icon set
# — NOT from our own table.  These are the codes called out in issue #44 plus a
# couple that anchor the day/night independence.
_PINNED = {
    1: "sunny",            # sunny (day)
    2: "partlycloudy",     # mostly sunny, some clouds — NOT sunny (#44)
    3: "partlycloudy",     # partly sunny, thick passing clouds
    12: "lightning",       # sunny intervals, chance of thunderstorms — NOT fog
    26: "sunny",           # high clouds — NOT snowy (#44)
    27: "fog",             # stratus — NOT rainy (#44)
    28: "fog",             # fog — NOT lightning-rainy (#44)
    29: "rainy",           # sunny intervals, scattered showers — NOT snowy (#44)
    35: "cloudy",          # overcast and dry — NOT pouring (#44)
    38: "lightning-rainy", # overcast, thundery showers — NOT hail (#44)
    101: "clear-night",    # clear (night)
    102: "partlycloudy",   # slightly overcast (night)
    126: "cloudy",         # high cloud at night — NOT sunny like day 26 (#44)
}


# ---------------------------------------------------------------------------
# Pinned mappings (non-self-referential)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "expected"), sorted(_PINNED.items()))
def test_pinned_code_maps_to_reference_condition(code: int, expected: str) -> None:
    assert condition_for_symbol(code) == expected


def test_night_meaning_is_independent_of_day() -> None:
    # 26 is "high clouds" (sunny) by day but "high cloud" (cloudy) at night —
    # night codes must be mapped from their own entries, not code - 100.
    assert condition_for_symbol(26) == "sunny"
    assert condition_for_symbol(126) == "cloudy"


# ---------------------------------------------------------------------------
# Table completeness — every code 1–42 and 101–142 must resolve
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [*range(1, 43), *range(101, 143)])
def test_every_expected_code_resolves(code: int) -> None:
    """Gaps would make the entity report no condition at all (#44)."""
    result = condition_for_symbol(code)
    assert result is not None, f"Code {code} returned None"
    assert result in _VALID_CONDITIONS, f"Code {code} → unknown condition {result!r}"


def test_table_covers_exactly_the_expected_codes() -> None:
    assert set(_CONDITIONS) == {*range(1, 43), *range(101, 143)}


def test_every_table_condition_is_valid() -> None:
    for code, condition in _CONDITIONS.items():
        assert condition in _VALID_CONDITIONS, (
            f"Code {code} maps to unknown condition {condition!r}"
        )


# ---------------------------------------------------------------------------
# Day / night semantics
# ---------------------------------------------------------------------------


def test_is_daytime_false_uses_the_night_counterpart() -> None:
    # A daytime daily symbol rendered at night takes its own night code.
    assert condition_for_symbol(1, is_daytime=False) == "clear-night"   # → 101
    assert condition_for_symbol(26, is_daytime=False) == "cloudy"       # → 126
    assert condition_for_symbol(3, is_daytime=False) == "partlycloudy"  # → 103


def test_is_daytime_true_keeps_the_day_code() -> None:
    assert condition_for_symbol(1, is_daytime=True) == "sunny"
    assert condition_for_symbol(26, is_daytime=True) == "sunny"


def test_is_daytime_true_uses_the_day_counterpart() -> None:
    # An hourly night symbol still in the feed after sunrise takes its own day
    # code (#103): 101 → 1 is ``sunny``, 126 → 26 is ``sunny`` (not ``cloudy``).
    assert condition_for_symbol(101, is_daytime=True) == "sunny"
    assert condition_for_symbol(126, is_daytime=True) == "sunny"
    assert condition_for_symbol(105, is_daytime=True) == "cloudy"


def test_is_daytime_true_does_not_shift_an_already_day_code() -> None:
    # The hint must not subtract 100 from a day code.
    assert condition_for_symbol(1, is_daytime=True) == "sunny"
    assert condition_for_symbol(42, is_daytime=True) == "snowy"


def test_is_daytime_none_keeps_the_night_code() -> None:
    assert condition_for_symbol(101, is_daytime=None) == "clear-night"
    assert condition_for_symbol(126, is_daytime=None) == "cloudy"


def test_is_daytime_none_keeps_the_day_code() -> None:
    assert condition_for_symbol(1, is_daytime=None) == "sunny"
    assert condition_for_symbol(26, is_daytime=None) == "sunny"


def test_is_daytime_does_not_shift_an_already_night_code() -> None:
    # Night codes are looked up directly; the hint must not add another 100.
    assert condition_for_symbol(101, is_daytime=False) == "clear-night"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_none_input_returns_none() -> None:
    assert condition_for_symbol(None) is None


def test_unknown_code_returns_none() -> None:
    assert condition_for_symbol(999) is None


def test_unknown_night_code_returns_none() -> None:
    # A night code past the table (143–199) has no entry.
    assert condition_for_symbol(199) is None
    assert condition_for_symbol(143) is None


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
