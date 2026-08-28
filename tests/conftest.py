"""Shared pytest fixtures for the MeteoSwiss Weather integration tests.

``enable_custom_integrations`` (autouse) ensures that every test which
requests the ``hass`` fixture loads the custom component from the repo tree
rather than from an installed package. Tests that do not use ``hass`` (the
pure-Python OGD tests) are unaffected because ``hass`` is never injected
for them.

``mock_ogd`` is an opt-in fixture for integration-level tests that need the
HA HTTP client (``aioclient_mock``) pre-seeded with the upstream URLs and
fixture files. The pure-Python OGD tests in ``test_ogd_stations.py`` and
``test_ogd_forecast.py`` use ``aioresponses`` directly (no HA) and do not
need this fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.meteoswiss_weather.ogd.const import (
    COLLECTION_FORECAST,
    DAILY_REQUIRED_PARAMS,
    DAILY_WIND_PARAMS,
    HOURLY_REQUIRED_PARAMS,
    META_DATAINVENTORY_URL,
    META_POINT_URL,
    META_STATIONS_URL,
    stac_items_url,
    station_now_url,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Run timestamp present in the fixture files (2026-08-27 02:00 UTC).
_RUN_TS = "202608270200"
_ASSET_BASE = (
    "https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/20260827-ch"
)


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable the custom integration for every test that uses ``hass``.

    Wraps the plugin's ``enable_custom_integrations`` as autouse so callers
    do not need to remember to request it explicitly. Tests that never touch
    ``hass`` are unaffected.
    """


def _register_mock_ogd(
    aioclient_mock,
    *,
    datainventory_content: bytes | None = None,
) -> None:
    """Register upstream URL mocks on ``aioclient_mock``.

    ``datainventory_content`` overrides the default fixture bytes for the data
    inventory, enabling tests that need a reduced parameter set for the station.
    """
    # Station metadata — downloaded once, cached.
    aioclient_mock.get(
        META_STATIONS_URL,
        content=_fixture_bytes("ogd-smn_meta_stations.csv"),
    )

    # Data inventory — which parameters each station actually measures.
    aioclient_mock.get(
        META_DATAINVENTORY_URL,
        content=(
            datainventory_content
            if datainventory_content is not None
            else _fixture_bytes("ogd-smn_meta_datainventory.csv")
        ),
    )

    # BER 10-minute observation file (closest station to Bern city centre).
    aioclient_mock.get(
        station_now_url("ber"),
        content=_fixture_bytes("ogd-smn_ber_t_now.csv"),
    )

    # Forecast point metadata — downloaded once, cached.
    aioclient_mock.get(
        META_POINT_URL,
        content=_fixture_bytes("ogd-local-forecasting_meta_point.csv"),
    )

    # STAC items listing for the local-forecast collection.
    aioclient_mock.get(
        stac_items_url(COLLECTION_FORECAST),
        content=_fixture_bytes("ogd-local-forecasting_items.json"),
    )

    # Daily parameter CSV files for the fixture run.
    for param in DAILY_REQUIRED_PARAMS:
        aioclient_mock.get(
            f"{_ASSET_BASE}/vnut12.lssw.{_RUN_TS}.{param}.csv",
            content=_fixture_bytes(f"vnut12.lssw.{_RUN_TS}.{param}.csv"),
        )

    # Wind block files fetched with every daily refresh (issue #60). Registered
    # unconditionally; if layout detection returns non-point-major the daily wind
    # fields are None (guardrail fires), but the test entry remains valid.
    for param in DAILY_WIND_PARAMS:
        aioclient_mock.get(
            f"{_ASSET_BASE}/vnut12.lssw.{_RUN_TS}.{param}.csv",
            content=_fixture_bytes(f"vnut12.lssw.{_RUN_TS}.{param}.csv"),
        )

    # Hourly parameter CSV files (opt-in). Registered unconditionally so tests
    # can assert they are *not* fetched when the option is off; the default
    # config entry has no options, so nothing here is downloaded.
    for param in HOURLY_REQUIRED_PARAMS:
        aioclient_mock.get(
            f"{_ASSET_BASE}/vnut12.lssw.{_RUN_TS}.{param}.csv",
            content=_fixture_bytes(f"vnut12.lssw.{_RUN_TS}.{param}.csv"),
        )


# A minimal datainventory that gives BER only its precipitation parameter.
# Used by mock_ogd_reduced to exercise the sensor-filtering path (issue #46).
_BER_PRECIPITATION_ONLY_INVENTORY = (
    b"station_abbr;parameter_shortname;meas_cat_nr;data_since;data_till;owner\n"
    b"BER;rre150z0;1;01.02.2004 00:00;;MeteoSchweiz\n"
)


@pytest.fixture
def mock_ogd(aioclient_mock):
    """Pre-seed the HA HTTP client mocker with every upstream URL.

    Registers fixture-backed responses for the station metadata, data inventory,
    one per-station observation file (BER), the forecast point metadata, the STAC
    items listing, and the four daily parameter CSVs. Returns the mocker so
    callers can inspect call counts or add more routes.
    """
    _register_mock_ogd(aioclient_mock)
    return aioclient_mock


@pytest.fixture
def mock_ogd_reduced(aioclient_mock):
    """Like ``mock_ogd`` but BER has only its precipitation parameter.

    Used to exercise the reduced-sensor and orphan-cleanup paths (issue #46).
    """
    _register_mock_ogd(
        aioclient_mock,
        datainventory_content=_BER_PRECIPITATION_ONLY_INVENTORY,
    )
    return aioclient_mock
