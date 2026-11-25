"""What the ladder remembers about a file, and what one fetch returns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..const import FORECAST_ENCODING
from ..models import FileLayout


@dataclass(frozen=True, slots=True)
class RowGeometry:
    """Where a point's row sits in a date-major file (a hint, always verified).

    Every hour block of a date-major file lists the same points in the same
    order (measured 2026-09-18), so the point is ``row_offset`` bytes into
    every block, give or take the width of a few values. ``block_bytes`` is the
    mean size of one block over the span walked last time.
    ``anchor_stamp``/``anchor_offset`` name the first row found last time and
    ``end_stamp``/``end_offset`` the last one: byte offsets are stable across
    the runs of a UTC day, so the next fetch predicts its first row from the
    known rows around it instead of extrapolating across dozens of blocks,
    which misses by tens of KB once block sizes vary (issue #281).
    """

    block_bytes: float
    row_offset: int
    anchor_stamp: str | None = None
    anchor_offset: int | None = None
    end_stamp: str | None = None
    end_offset: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable form for persistence (issue #133)."""
        return {
            "block_bytes": self.block_bytes,
            "row_offset": self.row_offset,
            "anchor_stamp": self.anchor_stamp,
            "anchor_offset": self.anchor_offset,
            "end_stamp": self.end_stamp,
            "end_offset": self.end_offset,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RowGeometry:
        """Rebuild from :meth:`to_dict`; raises on a value it cannot trust.

        ``end_*`` are absent from hints stored before issue #281 and default to
        ``None``.
        """
        return cls(
            block_bytes=float(data["block_bytes"]),
            row_offset=int(data["row_offset"]),
            anchor_stamp=_opt_str(data.get("anchor_stamp")),
            anchor_offset=_opt_int(data.get("anchor_offset")),
            end_stamp=_opt_str(data.get("end_stamp")),
            end_offset=_opt_int(data.get("end_offset")),
        )


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True, slots=True)
class FileHint:
    """Everything learned about one file, remembered per UTC day (ADR-0008).

    A file's layout and byte offsets are stable across the runs of one UTC day
    (date-major files start at 21:00 UTC of the previous day, docs/ogd.md), so a
    same-day hint lets :func:`fetch_series` skip classification and the
    header/first/last probes and verify through the rows it finds instead.

    ``utc_day`` is the UTC day the offsets were learned on; a hint from another
    day is not trusted (a layout can flip between days without notice), so it
    costs a fresh look rather than a wrong or oversized read. ``header`` is the
    file's header line (with its trailing newline) so the point's rows can be
    re-emitted without reading it. ``block_start`` is the point-major block
    offset; ``geometry`` plus ``first_stamp``/``last_stamp`` are the date-major
    row-addressing hint. Only the fields that apply to the file's layout are set.
    """

    layout: FileLayout
    utc_day: date | None = None
    header: str = ""
    block_start: int | None = None
    geometry: RowGeometry | None = None
    first_stamp: str | None = None
    last_stamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serialisable form for persistence across restarts (issue #133).

        The layout enum becomes its string value and ``utc_day`` an ISO date;
        everything else is already JSON-native. Only ever a hint: the ladder
        re-verifies whatever :meth:`from_dict` hands back (ADR-0008).
        """
        return {
            "layout": self.layout.value,
            "utc_day": self.utc_day.isoformat() if self.utc_day else None,
            "header": self.header,
            "block_start": self.block_start,
            "geometry": self.geometry.to_dict() if self.geometry else None,
            "first_stamp": self.first_stamp,
            "last_stamp": self.last_stamp,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FileHint:
        """Rebuild from :meth:`to_dict`.

        Raises on anything it cannot trust (a bad ``layout`` value, a non-mapping
        geometry, a malformed date) so the caller
        (:meth:`~.backend.BulkCsvBackend.import_hints`) can drop just that entry —
        a corrupt store must cost a fresh look, never a wrong row (the ladder
        verifies every remembered position anyway).
        """
        geometry = data.get("geometry")
        utc_day = data.get("utc_day")
        return cls(
            layout=FileLayout(data["layout"]),
            utc_day=date.fromisoformat(utc_day) if utc_day else None,
            header=str(data.get("header", "")),
            block_start=_opt_int(data.get("block_start")),
            geometry=RowGeometry.from_dict(geometry) if geometry else None,
            first_stamp=_opt_str(data.get("first_stamp")),
            last_stamp=_opt_str(data.get("last_stamp")),
        )


@dataclass(frozen=True, slots=True)
class SeriesResult:
    """The point's rows of one file, and what it took to get them."""

    text: str  # header + only the point's rows, ready for the shared parsers
    layout: FileLayout
    level: int  # 0..4, the highest rung of the ladder that was needed
    requests: int
    bytes: int
    # True when ``text`` holds every row of the point in the file, so any
    # consumer may reuse it; False when it was cut to the demanded window.
    whole_run: bool
    # What was learned about the file, to hand back on the next fetch of the
    # same UTC day (one object per file, ADR-0008). ``None`` after an escalation
    # that learned no reusable position, so the caller keeps its previous hint.
    hint: FileHint | None = None

    @property
    def has_rows(self) -> bool:
        """Whether the file carried any row for the point."""
        return self.text.count("\n") > 1


def _hint_is_current(hint: FileHint, utc_day: date | None) -> bool:
    """Whether ``hint``'s remembered offsets can still be trusted.

    Offsets are stable only across the runs of one UTC day (ADR-0008). A hint
    from another day is not trusted — a layout can flip between days without
    notice — so it is dropped and the file gets a fresh classification. When the
    day of either side is unknown the rows found still verify the hint, so it is
    used but its offsets are re-anchored against what is actually there.
    """
    if hint.utc_day is None or utc_day is None:
        return True
    return hint.utc_day == utc_day


def _hint_header_bytes(hint: FileHint | None) -> bytes | None:
    """The header line of a same-day hint, or ``None`` to read it afresh."""
    if hint is None or not hint.header:
        return None
    return hint.header.encode(FORECAST_ENCODING)
