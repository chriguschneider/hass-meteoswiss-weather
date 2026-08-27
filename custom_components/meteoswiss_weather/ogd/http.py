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
