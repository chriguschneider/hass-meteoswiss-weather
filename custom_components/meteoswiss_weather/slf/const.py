"""Constants for the SLF IMIS client (ADR-0009).

Duplicated from the integration's ``const.py`` on purpose: this package
imports nothing from Home Assistant so it can move to PyPI unchanged.
Keep ``SLF_API_BASE`` here identical to the one in the top-level
``const.py``.
"""

from __future__ import annotations

# SLF IMIS measurement API (ADR-0009, docs/slf.md).
SLF_API_BASE = "https://measurement-api.slf.ch"

# The three endpoints the integration uses (ADR-0009).
STATIONS_URL = f"{SLF_API_BASE}/public/api/imis/stations"
DAILY_SNOW_URL = f"{SLF_API_BASE}/public/api/imis/daily-snow"


def measurements_url(code: str) -> str:
    """Full URL for one station's recent measurements (period_in_days=1)."""
    return (
        f"{SLF_API_BASE}/public/api/imis/station/{code}"
        "/measurements?period_in_days=1"
    )


# Station type that carries snow height on a flat study plot.
SNOW_FLAT = "SNOW_FLAT"
