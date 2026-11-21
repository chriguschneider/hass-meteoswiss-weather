"""Edge cases of the shared OGD CSV cell and timestamp helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_components.meteoswiss_weather.ogd.models import OgdParseError
from custom_components.meteoswiss_weather.ogd.parse import (
    csv_reader,
    parse_stamp,
    parse_timestamp,
    to_float,
    to_int,
)


def test_csv_reader_uses_semicolon() -> None:
    rows = list(csv_reader("a;b\n1;2\n"))
    assert rows == [{"a": "1", "b": "2"}]


@pytest.mark.parametrize("value", [None, "", "   ", "n/a", "1,5x"])
def test_to_float_no_value(value: str | None) -> None:
    assert to_float(value) is None
    assert to_int(value) is None


def test_to_float_and_int_values() -> None:
    assert to_float("12.5") == 12.5
    assert to_float(" 0 ") == 0.0
    assert to_int("7") == 7
    assert to_int("7.0") == 7


def test_parse_timestamp_ok() -> None:
    assert parse_timestamp("01.02.2026 10:20", "BER") == datetime(
        2026, 2, 1, 10, 20, tzinfo=UTC
    )


@pytest.mark.parametrize("value", [None, "", "  "])
def test_parse_timestamp_empty_names_the_subject(value: str | None) -> None:
    with pytest.raises(OgdParseError, match="BER: observation row has no timestamp"):
        parse_timestamp(value, "BER")
    with pytest.raises(
        OgdParseError, match="BER: pollen observation row has no timestamp"
    ):
        parse_timestamp(value, "BER", subject="pollen observation")


def test_parse_timestamp_bad() -> None:
    with pytest.raises(OgdParseError, match="BER: bad timestamp"):
        parse_timestamp("2026-02-01", "BER")


def test_parse_stamp() -> None:
    assert parse_stamp(" 202602011020 ") == datetime(2026, 2, 1, 10, 20, tzinfo=UTC)


@pytest.mark.parametrize("value", [None, "", "2026020110", "20260201102x"])
def test_parse_stamp_invalid(value: str | None) -> None:
    assert parse_stamp(value) is None
