"""The range-reader seam, row helpers and the request-counting reader."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import aiohttp

from ..const import HOURLY_ROW_PROBE_BYTES
from ..http import get_bytes

# ---------------------------------------------------------------------------
# The range-reader seam
# ---------------------------------------------------------------------------


class RangeReader(Protocol):
    """Random byte access to one upstream file (a Range GET per read)."""

    async def size(self) -> int: ...

    async def read(self, start: int, length: int) -> bytes: ...

    async def read_all(self) -> bytes: ...


class AiohttpRangeReader:
    """A :class:`RangeReader` backed by conditional HTTP Range requests.

    Learns the object size from the first probe's ``Content-Range``. A server
    that ignores ``Range`` and answers 200 with the whole body is handled
    transparently: the full body is cached and later reads slice it locally, so
    the strategies still work (they simply stop saving traffic).
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        limiter: asyncio.Semaphore | None = None,
    ) -> None:
        self._session = session
        self._url = url
        self._limiter = limiter
        self._size: int | None = None
        self._full: bytes | None = None

    async def _prime(self) -> None:
        resp = await get_bytes(
            self._session,
            self._url,
            start=0,
            end=HOURLY_ROW_PROBE_BYTES - 1,
            limiter=self._limiter,
        )
        if resp.status == 200:
            self._full = resp.body
            self._size = len(resp.body)
        else:
            self._size = (
                resp.total_size if resp.total_size is not None else len(resp.body)
            )

    async def size(self) -> int:
        if self._size is None:
            await self._prime()
        assert self._size is not None
        return self._size

    async def read(self, start: int, length: int) -> bytes:
        if length <= 0 or start < 0:
            return b""
        if self._full is not None:
            return self._full[start : start + length]
        resp = await get_bytes(
            self._session,
            self._url,
            start=start,
            end=start + length - 1,
            limiter=self._limiter,
        )
        if resp.status == 200:
            # The origin ignored the Range; cache the full body once.
            self._full = resp.body
            self._size = len(resp.body)
            return self._full[start : start + length]
        return resp.body

    async def read_all(self) -> bytes:
        if self._full is None:
            resp = await get_bytes(self._session, self._url, limiter=self._limiter)
            self._full = resp.body
            self._size = len(resp.body)
        return self._full


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Row:
    """A parsed data row plus the byte offset it starts at."""

    start: int
    point_id: int
    point_type_id: int
    date: str  # the raw YYYYMMDDHHMM stamp; lexicographic == chronological


def _parse_row(start: int, line: bytes) -> _Row | None:
    """Parse ``id;type;Date;value`` into a :class:`_Row`.

    Returns ``None`` for the header row or any line whose id/type are not
    integers, so a probe that lands in the header is simply "no row here".
    """
    parts = line.split(b";", 3)
    if len(parts) < 3:
        return None
    try:
        point_id = int(parts[0])
        point_type_id = int(parts[1])
    except ValueError:
        return None
    return _Row(start, point_id, point_type_id, parts[2].decode("ascii", "ignore"))


async def _read_row_after(reader: RangeReader, offset: int) -> _Row | None:
    """First complete data row that starts after the newline at/after ``offset``.

    Monotonic in ``offset`` (the basis for the binary search) and always
    returns a whole row: it reads from ``offset`` until it has seen the row's
    opening and closing newline. ``None`` past the last row.
    """
    size = await reader.size()
    if offset >= size:
        return None
    data = b""
    pos = offset
    first_nl = -1
    while True:
        piece = await reader.read(pos, HOURLY_ROW_PROBE_BYTES)
        if not piece:
            break
        data += piece
        pos += len(piece)
        if first_nl == -1:
            first_nl = data.find(b"\n")
        if first_nl != -1 and data.find(b"\n", first_nl + 1) != -1:
            break
        if pos >= size:
            break
    if first_nl == -1:
        return None
    row_start_local = first_nl + 1
    second_nl = data.find(b"\n", row_start_local)
    if second_nl == -1:
        if pos < size:
            return None  # row longer than we read and not at EOF
        line = data[row_start_local:]
    else:
        line = data[row_start_local:second_nl]
    return _parse_row(offset + row_start_local, line)


async def _read_row_before(reader: RangeReader, offset: int) -> _Row | None:
    """The last complete data row that ends at/before ``offset``.

    Used to confirm a cached block start really is the point's *first* row.
    ``None`` when the window did not reach a row boundary (caller re-searches).
    """
    if offset <= 0:
        return None
    w0 = max(0, offset - HOURLY_ROW_PROBE_BYTES)
    data = await reader.read(w0, offset - w0)
    data = data.rstrip(b"\n")
    nl = data.rfind(b"\n")
    if nl == -1:
        if w0 == 0:
            return _parse_row(0, data)  # header or the very first row
        return None
    return _parse_row(w0 + nl + 1, data[nl + 1 :])


async def _read_header(reader: RangeReader) -> bytes:
    """The file's header line, including its trailing newline."""
    pos = 0
    data = b""
    size = await reader.size()
    while True:
        piece = await reader.read(pos, HOURLY_ROW_PROBE_BYTES)
        if not piece:
            break
        data += piece
        pos += len(piece)
        nl = data.find(b"\n")
        if nl != -1:
            return data[: nl + 1]
        if pos >= size:
            break
    return data

# ---------------------------------------------------------------------------
# Ladder control flow and the request-counting reader
# ---------------------------------------------------------------------------

class _NotProven(Exception):
    """A level could not prove it delivered every demanded row; climb."""


class _RequestCapExceeded(_NotProven):
    """A level needed more requests than the cap allows; climb.

    ``window`` is the demand's resolved ``(start, end)`` (aware UTC): the
    explicit window of a windowed demand, or a whole-run demand's own extent
    (the file's first stamp to one step past its last). It lets a date-major
    file that overran the cap be re-fetched as consecutive cap-sized
    row-addressed windows instead of a prefix or the whole file.
    """

    def __init__(
        self, message: str, *, window: tuple[datetime, datetime] | None = None
    ) -> None:
        super().__init__(message)
        self.window = window

class _CountingReader:
    """A :class:`RangeReader` that counts requests/bytes and enforces a cap."""

    def __init__(self, inner: RangeReader, cap: int | None) -> None:
        self._inner = inner
        self.cap = cap
        self.requests = 0
        self.bytes = 0

    def _charge(self) -> None:
        self.requests += 1
        if self.cap is not None and self.requests > self.cap:
            raise _RequestCapExceeded(f"more than {self.cap} requests")

    async def size(self) -> int:
        return await self._inner.size()

    async def read(self, start: int, length: int) -> bytes:
        self._charge()
        data = await self._inner.read(start, length)
        self.bytes += len(data)
        return data

    async def read_all(self) -> bytes:
        self._charge()
        data = await self._inner.read_all()
        self.bytes += len(data)
        return data
