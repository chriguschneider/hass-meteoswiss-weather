"""Layout classification: date-major, point-major or fallback."""

from __future__ import annotations

from ..models import FileLayout, ForecastPoint
from .reader import RangeReader, _read_row_after, _Row

# Number of evenly spaced byte offsets sampled to classify a file's layout.
# The tiny probes are cheap, and extra samples make the monotonicity verdict
# robust on a shuffled/unexpected file.
_LAYOUT_PROBES = 9


# ---------------------------------------------------------------------------
# Layout classification
# ---------------------------------------------------------------------------


def _nondecreasing(values: list) -> bool:
    return all(a <= b for a, b in zip(values, values[1:], strict=False))


def _increasing(values: list) -> bool:
    return all(a < b for a, b in zip(values, values[1:], strict=False))


async def _adjacent_rows_date_major(reader: RangeReader, rows: list[_Row]) -> bool:
    """Whether a probe row and the row right after it share a date, not a point.

    Inside a date-major block consecutive rows are different points on the same
    date; inside a point-major block they are the same point on successive
    dates. A pair that straddles a block boundary differs in both and decides
    nothing, so the next probe is tried.
    """
    for row in rows:
        nxt = await _read_row_after(reader, row.start)
        if nxt is None:
            continue
        same_point = (nxt.point_type_id, nxt.point_id) == (
            row.point_type_id,
            row.point_id,
        )
        if same_point:
            return False
        if nxt.date == row.date:
            return True
    return False


async def classify_layout(reader: RangeReader) -> FileLayout:
    """Detect a file's row order from evenly spaced byte-offset probes.

    Date-major files show a non-decreasing ``Date`` across the file; the two
    point-major variants show a non-decreasing ``(type, id)`` or ``id`` key;
    anything else (e.g. a shuffled file) is :data:`FileLayout.FALLBACK`, which
    the caller downloads in full.
    """
    size = await reader.size()
    if size == 0:
        return FileLayout.FALLBACK
    offsets = [size * i // _LAYOUT_PROBES for i in range(_LAYOUT_PROBES)]
    rows: list[_Row] = []
    for off in offsets:
        row = await _read_row_after(reader, off)
        if row is not None and (not rows or row.start != rows[-1].start):
            rows.append(row)
    if len(rows) < 3:
        return FileLayout.FALLBACK

    dates = [r.date for r in rows]
    type_keys = [(r.point_type_id, r.point_id) for r in rows]
    ids = [r.point_id for r in rows]

    # A many-block date-major file (the hourly files, ~220 hour blocks) has
    # every widely spaced probe in a different block, so the dates strictly
    # increase; requiring strict monotonicity here makes a false date-major
    # verdict on a point-major file — the one dangerous misclassification, since
    # it would drop the point's later hours — vanishingly unlikely.
    if _increasing(dates):
        return FileLayout.DATE_MAJOR
    point_major: FileLayout | None = None
    if _nondecreasing(type_keys) and len(set(type_keys)) > 1:
        point_major = FileLayout.POINT_MAJOR_TYPE
    elif _nondecreasing(ids) and len(set(ids)) > 1:
        point_major = FileLayout.POINT_MAJOR_ID
    if point_major is not None:
        # A few-block date-major file can alias with the probes: with about one
        # probe per day block, each probe lands slightly deeper into its block
        # than the last, so the point keys creep upward and look sorted
        # (tre200pn, run 202610140400 — every forecast day but one lost its
        # minimum). When the dates are non-decreasing too, the probes cannot
        # tell the layouts apart; a row's successor can.
        if _nondecreasing(dates) and await _adjacent_rows_date_major(reader, rows):
            return FileLayout.DATE_MAJOR
        return point_major
    # A few-block date-major file (the daily p-variants, only nine day blocks)
    # has more probes than blocks, so its dates are non-decreasing with repeats
    # rather than strictly increasing (issue #122). This branch is reached only
    # after the point-major keys are shown *not* to be monotonic, so a genuine
    # point-major file (whose sort key is monotonic by construction) is caught
    # above and never lands here — the dangerous misclassification stays ruled
    # out. A file that reaches here and is actually point-major would still be
    # caught by the addressing's completeness check and climb, not silently
    # truncate.
    if _nondecreasing(dates) and len(set(dates)) > 1:
        return FileLayout.DATE_MAJOR
    return FileLayout.FALLBACK


def _row_key(layout: FileLayout, row: _Row) -> tuple:
    if layout is FileLayout.POINT_MAJOR_TYPE:
        return (row.point_type_id, row.point_id)
    return (row.point_id,)


def _target_key(layout: FileLayout, point: ForecastPoint) -> tuple:
    if layout is FileLayout.POINT_MAJOR_TYPE:
        return (point.point_type_id, point.point_id)
    return (point.point_id,)
