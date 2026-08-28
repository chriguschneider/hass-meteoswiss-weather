"""Conditional HTTP text fetch, shared by the station and forecast clients.

One helper, ``get_text``, that carries an ``ETag`` / ``Last-Modified`` pair
across calls so an unchanged file costs a single 304 (ADR-0002). Reused by
the current-observations client (#4) and the forecast client (#10).
"""

from __future__ import annotations

from dataclasses import dataclass

import aiohttp

from .models import OgdConnectionError

# aiohttp's default has no ceiling; a stuck socket must not hang a poll.
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)


@dataclass(slots=True)
class CachedResponse:
    """A fetched body plus the validators needed to revalidate it.

    Passed back into ``get_text`` on the next poll: the helper mutates it
    in place on a 200 and leaves it untouched on a 304, so a caller only
    has to hold on to the same object.
    """

    body: str
    etag: str | None = None
    last_modified: str | None = None


async def get_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    cache: CachedResponse | None = None,
    timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
    encoding: str = "utf-8",
) -> CachedResponse:
    """Fetch ``url`` as text, revalidating against ``cache`` when given.

    Returns a :class:`CachedResponse`. When ``cache`` is supplied its
    validators are sent as ``If-None-Match`` / ``If-Modified-Since``; on a
    304 the same object is returned unchanged, on a 200 it is updated in
    place and returned. Any transport error or non-2xx/304 status raises
    :class:`OgdConnectionError`.
    """
    headers: dict[str, str] = {}
    if cache is not None:
        if cache.etag:
            headers["If-None-Match"] = cache.etag
        if cache.last_modified:
            headers["If-Modified-Since"] = cache.last_modified

    try:
        async with session.get(url, headers=headers, timeout=timeout) as response:
            if cache is not None and response.status == 304:
                return cache
            if response.status != 200:
                raise OgdConnectionError(f"GET {url} returned HTTP {response.status}")
            # MeteoSwiss serves Latin-family encodings, not the charset the
            # response header claims; decode with the caller's encoding.
            raw = await response.read()
            body = raw.decode(encoding, errors="replace")
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
    except aiohttp.ClientError as err:
        raise OgdConnectionError(f"GET {url} failed: {err}") from err

    if cache is not None:
        cache.body = body
        cache.etag = etag
        cache.last_modified = last_modified
        return cache
    return CachedResponse(body=body, etag=etag, last_modified=last_modified)


@dataclass(slots=True)
class RangeResponse:
    """The result of a (possibly partial) byte fetch.

    ``status`` is the HTTP status (200 full, 206 partial, 304 unchanged).
    ``total_size`` is the full object size when the server disclosed it
    (``Content-Range`` on a 206, ``Content-Length`` on a 200), else ``None``.
    ``body`` holds the returned bytes and is empty on a 304.
    """

    status: int
    body: bytes
    etag: str | None = None
    total_size: int | None = None


def _parse_total_size(content_range: str | None) -> int | None:
    """Total object size from a ``Content-Range: bytes a-b/total`` header."""
    if not content_range or "/" not in content_range:
        return None
    total = content_range.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None


async def get_bytes(
    session: aiohttp.ClientSession,
    url: str,
    *,
    start: int | None = None,
    end: int | None = None,
    etag: str | None = None,
    timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
) -> RangeResponse:
    """Fetch ``url`` as bytes, optionally only the ``start``–``end`` range.

    ``start``/``end`` (inclusive, HTTP ``Range`` semantics) request a byte
    range; the origin answers 206 with ``Content-Range`` (the hourly bulk
    files honour this, docs/ogd.md §E4). ``etag`` is sent as ``If-None-Match``,
    so an unchanged object answers 304 — this keeps working together with a
    ``Range`` (measured, issue #50). A server that ignores ``Range`` and
    answers 200 with the full body is handled by the caller (the range reader
    caches the body and slices locally). Any transport error or unexpected
    status raises :class:`OgdConnectionError`.
    """
    headers: dict[str, str] = {}
    if start is not None or end is not None:
        headers["Range"] = f"bytes={start if start is not None else 0}-" + (
            "" if end is None else str(end)
        )
    if etag:
        headers["If-None-Match"] = etag

    try:
        async with session.get(url, headers=headers, timeout=timeout) as response:
            status = response.status
            if status == 304:
                return RangeResponse(status=304, body=b"", etag=etag)
            if status not in (200, 206):
                raise OgdConnectionError(f"GET {url} returned HTTP {status}")
            body = await response.read()
            resp_etag = response.headers.get("ETag")
            if status == 206:
                total = _parse_total_size(response.headers.get("Content-Range"))
            else:
                length = response.headers.get("Content-Length")
                total = int(length) if length and length.isdigit() else len(body)
    except aiohttp.ClientError as err:
        raise OgdConnectionError(f"GET {url} failed: {err}") from err

    return RangeResponse(status=status, body=body, etag=resp_etag, total_size=total)
