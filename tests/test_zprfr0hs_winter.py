"""Winter ``zprfr0hs`` stays on row addressing (issue #281).

From late November the zero-degree level drops below 1000 m, so the file mixes
3- and 4-digit values and its hour blocks differ in size (153 031–153 341 B per
block on run ``202611251200``, 5821 points; docs/ogd.md §E4). A same-day hint
from the previous refresh then mispredicted the first row of a long window,
the fresh look that followed overran the 96-request cap, and every refresh fell
to the ~30 MB prefix (level 3) while keeping the stale hint.

The fixture is that run, thinned to every 16th row of each block plus the
point's own row (``tests/README.md``): real values, real order, real widths.
Because each block is ~16 times smaller, the read windows of the row walk are
scaled by the same factor, so the walk sees the same relative drift as on the
full file.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.meteoswiss_weather.ogd import hourly as H
from custom_components.meteoswiss_weather.ogd.const import SERIES_REQUEST_CAP
from custom_components.meteoswiss_weather.ogd.hourly import rows as H_rows
from custom_components.meteoswiss_weather.ogd.models import FileLayout, ForecastPoint

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "vnut12.lssw.202611251200.zprfr0hs.thinned.csv"
)
_KEEP_EVERY = 16  # tests/tools/refresh_fixtures.py ZPRFR0HS_KEEP_EVERY
_POINT = ForecastPoint(309800, 2, "3098", "Köniz", 46.9, 7.4, 595.0)
_DAY = datetime(2026, 11, 25, tzinfo=UTC).date()
_FIRST = datetime(2026, 11, 24, 21, tzinfo=UTC)  # the file's first block
_START = datetime(2026, 11, 25, 0, tzinfo=UTC)
_END = _START + timedelta(hours=48)
_ONE_HOUR = timedelta(hours=1)

# The geometry measured on 2026-09-18 (docs/ogd.md §E4, ADR-0008 "Verified"):
# 5632 points per block, 148 428 B per block, the point at row index 2832.
# Scaled like the fixture.
_OLD_BLOCK = 148_428 / _KEEP_EVERY
_OLD_ROW_OFFSET = int(2832 * 148_428 / 5632 / _KEEP_EVERY)


@pytest.fixture(autouse=True)
def _scaled_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scale the walk's byte windows to the thinned blocks."""
    monkeypatch.setattr(
        H_rows,
        "_ROW_WINDOWS",
        tuple(w // _KEEP_EVERY for w in H_rows._ROW_WINDOWS),
    )
    monkeypatch.setattr(
        H_rows, "_LEARN_CHUNK_BYTES", H_rows._LEARN_CHUNK_BYTES // _KEEP_EVERY
    )


class _MemReader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def size(self) -> int:
        return len(self._data)

    async def read(self, start: int, length: int) -> bytes:
        return self._data[start : start + length]

    async def read_all(self) -> bytes:
        return self._data


async def _fetch(
    data: bytes,
    *,
    start: datetime = _START,
    end: datetime = _END,
    hint: H.FileHint | None = None,
    cap: int = SERIES_REQUEST_CAP,
) -> tuple[H.SeriesResult, list[H._CountingReader]]:
    """The public path (single shot, chunked when over the cap)."""
    readers: list[H._CountingReader] = []

    def make_reader() -> H._CountingReader:
        reader = H._CountingReader(_MemReader(data), cap)
        readers.append(reader)
        return reader

    result = await H._fetch_series_auto(
        make_reader,
        _POINT,
        window_start=start,
        window_end=end,
        hint=hint,
        utc_day=_DAY,
        step=_ONE_HOUR,
        request_cap=cap,
    )
    return result, readers


def _hours(result: H.SeriesResult) -> list[str]:
    return [line.split(";")[2] for line in result.text.split("\n")[1:] if line]


def _expected(data: bytes, start: datetime, end: datetime) -> list[str]:
    full = H._filter_point_rows(data.decode("iso-8859-1"), _POINT)
    lo, hi = start.strftime("%Y%m%d%H%M"), end.strftime("%Y%m%d%H%M")
    return [
        line
        for line in full.split("\n")[1:]
        if line and lo <= line.split(";")[2] < hi
    ]


def _old_geometry_hint(data: bytes) -> H.FileHint:
    """A same-day hint carrying the 5632-point geometry of September."""
    header = data.split(b"\n", 1)[0].decode("iso-8859-1") + "\n"
    anchor = _START + timedelta(hours=6)
    return H.FileHint(
        layout=FileLayout.DATE_MAJOR,
        utc_day=_DAY,
        header=header,
        first_stamp=_FIRST.strftime("%Y%m%d%H%M"),
        last_stamp="202611262300",
        geometry=H.RowGeometry(
            block_bytes=_OLD_BLOCK,
            row_offset=_OLD_ROW_OFFSET,
            anchor_stamp=anchor.strftime("%Y%m%d%H%M"),
            anchor_offset=len(header)
            + _OLD_ROW_OFFSET
            + int((anchor - _FIRST) / _ONE_HOUR * _OLD_BLOCK),
        ),
    )


def test_fixture_has_mixed_widths_and_varying_blocks() -> None:
    """The fixture carries the property under test (guards a careless refresh)."""
    data = _FIXTURE.read_bytes()
    widths: set[int] = set()
    sizes: dict[str, int] = {}
    for line in data.split(b"\n")[1:]:
        if line:
            parts = line.split(b";")
            widths.add(len(parts[3]))
            sizes[parts[2].decode()] = sizes.get(parts[2].decode(), 0) + len(line) + 1
    assert {3, 4} <= widths
    assert max(sizes.values()) - min(sizes.values()) > 100


async def test_48h_window_is_row_addressed_under_the_cap() -> None:
    data = _FIXTURE.read_bytes()
    result, readers = await _fetch(data)

    assert result.layout is FileLayout.DATE_MAJOR
    assert result.level <= 2
    assert all(r.requests <= SERIES_REQUEST_CAP for r in readers)
    assert _hours(result) == [
        (_START + timedelta(hours=h)).strftime("%Y%m%d%H%M") for h in range(48)
    ]
    assert [ln for ln in result.text.split("\n")[1:] if ln] == _expected(
        data, _START, _END
    )
    assert result.bytes < len(data) / 4


async def test_old_geometry_hint_is_dropped_and_relearned_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hint from the 5632-point layout costs one fresh look, never a prefix,
    and the geometry learned instead verifies on the next refresh."""
    data = _FIXTURE.read_bytes()
    caplog.set_level(logging.DEBUG, logger="custom_components.meteoswiss_weather")

    first, readers = await _fetch(data, hint=_old_geometry_hint(data))
    assert first.level <= 2
    assert all(r.requests <= SERIES_REQUEST_CAP for r in readers)
    assert _hours(first) == _hours((await _fetch(data))[0])
    assert first.hint is not None and first.hint.geometry is not None
    assert first.hint.geometry.block_bytes != _OLD_BLOCK
    assert first.hint.geometry.row_offset != _OLD_ROW_OFFSET
    assert sum("hint did not verify" in m for m in caplog.messages) <= 1

    caplog.clear()
    later = _START + timedelta(hours=1)
    second, _ = await _fetch(
        data, start=later, end=later + timedelta(hours=48), hint=first.hint
    )
    assert second.level <= 1
    assert not any("hint did not verify" in m for m in caplog.messages)
    assert not any("escalates" in m for m in caplog.messages)
    assert len(_hours(second)) == 47  # the fixture ends at 2026-11-26 23:00


async def test_chunked_refresh_hint_verifies_on_the_next_refresh(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The production chain: a window chunked under the cap, then the next
    refresh with the hint it learned. With a cap this small the 48 h window
    needs two chunks, like the ~80 h covering window under the real cap."""
    data = _FIXTURE.read_bytes()
    caplog.set_level(logging.DEBUG, logger="custom_components.meteoswiss_weather")
    cap = 50

    first, _ = await _fetch(data, cap=cap)
    assert first.level <= 2
    assert len(_hours(first)) == 48
    geometry = first.hint.geometry if first.hint else None
    # The merged hint is anchored on the start of the demand, not on the start
    # of the last chunk.
    assert geometry is not None and geometry.anchor_stamp == "202611250000"

    caplog.clear()
    later = _START + timedelta(hours=1)
    second, readers = await _fetch(
        data, start=later, end=later + timedelta(hours=47), hint=first.hint, cap=cap
    )
    assert second.level <= 2
    assert all(r.requests <= cap for r in readers)
    assert len(_hours(second)) == 47
    assert not any("hint did not verify" in m for m in caplog.messages)


async def test_failed_hint_is_replaced_when_the_fetch_escalates() -> None:
    """When the rows really are missing the ladder climbs, but the stale hint
    is replaced by a positions-free one, so it is not tried again every
    refresh."""
    data = _FIXTURE.read_bytes()
    gone = f"\n{_POINT.point_id};{_POINT.point_type_id};202611251200;".encode()
    at = data.index(gone) + 1
    broken = data[:at] + data[data.index(b"\n", at) + 1 :]

    result, _ = await _fetch(broken, hint=_old_geometry_hint(broken))

    assert result.level >= 3
    assert result.hint is not None
    assert result.hint.layout is FileLayout.DATE_MAJOR
    assert result.hint.geometry is None
    assert result.hint.utc_day == _DAY


def test_hint_stored_before_the_end_row_still_loads() -> None:
    """Hints persisted before #281 have no ``end_*`` keys."""
    geometry = H.RowGeometry.from_dict(
        {"block_bytes": 1.0, "row_offset": 2, "anchor_stamp": None,
         "anchor_offset": None}
    )
    assert geometry.end_stamp is None and geometry.end_offset is None
    assert H.RowGeometry.from_dict(geometry.to_dict()) == geometry
