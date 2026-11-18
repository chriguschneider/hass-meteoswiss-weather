"""A request timeout surfaces as OgdConnectionError / SlfConnectionError (#243).

aiohttp raises a bare ``TimeoutError`` (not an ``aiohttp.ClientError``) when
the request timeout fires; callers only catch the package's own errors.
"""

from __future__ import annotations

import pytest

from custom_components.meteoswiss_weather.ogd.http import get_bytes, get_text
from custom_components.meteoswiss_weather.ogd.models import OgdConnectionError
from custom_components.meteoswiss_weather.slf.client import _fetch_imis_text
from custom_components.meteoswiss_weather.slf.models import SlfConnectionError


class _TimeoutSession:
    """A session whose ``get`` raises ``TimeoutError`` like a fired timeout."""

    def get(self, *args, **kwargs):
        raise TimeoutError


async def test_get_text_timeout_is_connection_error() -> None:
    with pytest.raises(OgdConnectionError, match="timed out"):
        await get_text(_TimeoutSession(), "https://example.invalid/a.csv")


async def test_get_bytes_timeout_is_connection_error() -> None:
    with pytest.raises(OgdConnectionError, match="timed out"):
        await get_bytes(_TimeoutSession(), "https://example.invalid/a.csv")


async def test_fetch_imis_text_timeout_is_connection_error() -> None:
    with pytest.raises(SlfConnectionError, match="timed out"):
        await _fetch_imis_text(_TimeoutSession(), "https://example.invalid/x")
