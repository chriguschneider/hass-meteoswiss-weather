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

from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.meteoswiss_weather.ogd.const import (
    COLLECTION_FORECAST,
    DAILY_BLOCK_PARAMS,
    DAILY_REQUIRED_PARAMS,
    HOURLY_CLOUD_PARAMS,
    HOURLY_REQUIRED_PARAMS,
    HOURLY_SUNSHINE,
    HOURLY_TEMP_PERCENTILE_PARAMS,
    META_DATAINVENTORY_URL,
    META_POINT_URL,
    META_POLLEN_DATAINVENTORY_URL,
    META_PRECIP_STATIONS_URL,
    META_STATIONS_URL,
    pollen_now_url,
    precip_station_now_url,
    stac_day_item_url,
    stac_items_url,
    station_now_url,
)
from custom_components.meteoswiss_weather.slf.const import (
    DAILY_SNOW_URL,
    measurements_url,
)

FIXTURES = Path(__file__).parent / "fixtures"

# Run timestamp present in the fixture files (2026-08-27 02:00 UTC).
_RUN_TS = "202608270200"
_ASSET_BASE = (
    "https://data.geo.admin.ch/ch.meteoschweiz.ogd-local-forecasting/20260827-ch"
)


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# The real sunshine fixture is from run 202610190800 (docs/ogd.md, "Sunshine
# duration"); the daily-path tests all use the fixture run 202608270200. There
# is no hand-written sunshine fixture for that run — its rows are derived from
# the real one by shifting every Date back a whole number of days so they land
# in the fixture week. Whole days preserve the hour-of-day pattern (local
# midnight alignment, the sunshine day/night shape), so the derived file is a
# faithful point-major block: 53 days is the gap between the two run dates
# (2026-10-19 → 2026-08-27).
_SUNSHINE_REAL_FIXTURE = "vnut12.lssw.202610190800.sre000h0.csv"
_SUNSHINE_REDATE_DAYS = 53


def _redated_sunshine_bytes() -> bytes:
    """The real sunshine fixture re-dated into the fixture run's week.

    Every ``Date`` cell (``YYYYMMDDHHMM`` UTC) is shifted back
    :data:`_SUNSHINE_REDATE_DAYS` whole days; nothing else changes, so the
    point-major row order and the integer sunshine values are the real ones.
    """
    from datetime import timedelta

    shift = timedelta(days=_SUNSHINE_REDATE_DAYS)
    text = (FIXTURES / _SUNSHINE_REAL_FIXTURE).read_text(encoding="iso-8859-1")
    lines = text.splitlines()
    out = [lines[0]]  # header
    for line in lines[1:]:
        if not line:
            continue
        point_id, point_type_id, stamp, value = line.split(";")
        when = datetime(
            int(stamp[:4]),
            int(stamp[4:6]),
            int(stamp[6:8]),
            int(stamp[8:10]),
            int(stamp[10:12]),
            tzinfo=UTC,
        ) - shift
        out.append(
            f"{point_id};{point_type_id};{when.strftime('%Y%m%d%H%M')};{value}"
        )
    return ("\n".join(out) + "\n").encode("iso-8859-1")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable the custom integration for every test that uses ``hass``.

    Wraps the plugin's ``enable_custom_integrations`` as autouse so callers
    do not need to remember to request it explicitly. Tests that never touch
    ``hass`` are unaffected.
    """


_POLLEN_STATION_ABBR = "pbe"  # lowercase for URL; PBE is closest to Bern in fixture


def _register_mock_ogd(
    aioclient_mock,
    *,
    datainventory_content: bytes | None = None,
    pollen_station: str | None = None,
    pollen_now_content: bytes | None = None,
    precip_station: str | None = None,
    imis_station: str | None = None,
) -> None:
    """Register upstream URL mocks on ``aioclient_mock``.

    ``datainventory_content`` overrides the default fixture bytes for the SMN
    data inventory, enabling tests that need a reduced parameter set.
    ``pollen_station`` (lowercase abbr, e.g. "pbe") activates pollen mocks:
    registers the pollen datainventory and the station's ``_h_now.csv``.
    ``pollen_now_content`` overrides the pollen fixture (e.g. all-empty rows).
    ``precip_station`` (lowercase abbr, e.g. "bel") activates the optional
    precipitation-station mocks (ADR-0006): the precip meta stations list and
    the station's ``_t_now.csv``.
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

    # Day item URLs for the primary discovery path (issue #120, ADR-0008).
    # The fixture carries the same run stamp (202608270200) for all dates.
    # Register all dates that freeze_time-based tests need, plus actual today
    # for tests that do not freeze time.
    _DAY_ITEM_DATES = {
        "20260827",  # fixture week (most tests)
        "20260828",  # test_sensor _DAY1
        "20260829",  # test_weather daytime/nighttime
        "20260910",  # test_sensor zero-degree-outside-run
        datetime.now(UTC).strftime("%Y%m%d"),  # actual today for unfrozen tests
    }
    for _date_str in _DAY_ITEM_DATES:
        aioclient_mock.get(
            stac_day_item_url(COLLECTION_FORECAST, f"{_date_str}-ch"),
            content=_fixture_bytes("ogd-local-forecasting_20260827-ch.json"),
        )

    # STAC items listing for the local-forecast collection (last-resort fallback).
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

    # Point-major block files fetched with every daily refresh: the three wind
    # files (issue #60) and the zero-degree level (issue #107). Registered
    # unconditionally. The wind fixtures are date-major, so their guardrail
    # fires and the daily wind fields are None; the zero-degree fixture keeps
    # the measured point-major order, so the sensor gets a value.
    # ``sre000h0`` (sunshine, ADR-0010) has no fixture for this run, so its rows
    # are derived from the real 202610190800 fixture (see _redated_sunshine_bytes).
    for param in DAILY_BLOCK_PARAMS:
        aioclient_mock.get(
            f"{_ASSET_BASE}/vnut12.lssw.{_RUN_TS}.{param}.csv",
            content=(
                _redated_sunshine_bytes()
                if param == HOURLY_SUNSHINE
                else _fixture_bytes(f"vnut12.lssw.{_RUN_TS}.{param}.csv")
            ),
        )

    # Hourly parameter CSV files (opt-in). Registered unconditionally so tests
    # can assert they are *not* fetched when the option is off; the default
    # config entry has no options, so nothing here is downloaded. The B9/B11
    # gated date-major additions (issue #69) join the list so a test with those
    # options on finds their files, while a plain hourly test never fetches them.
    hourly_params = (
        *HOURLY_REQUIRED_PARAMS,
        *HOURLY_CLOUD_PARAMS,
        *HOURLY_TEMP_PERCENTILE_PARAMS,
    )
    for param in hourly_params:
        aioclient_mock.get(
            f"{_ASSET_BASE}/vnut12.lssw.{_RUN_TS}.{param}.csv",
            content=_fixture_bytes(f"vnut12.lssw.{_RUN_TS}.{param}.csv"),
        )

    # Precipitation-station mocks (opt-in, ADR-0006): only registered when a
    # precip station is given. The meta list lets the config flow offer the
    # pick; the ``_t_now.csv`` backs the second 10-minute poll.
    if precip_station is not None:
        aioclient_mock.get(
            META_PRECIP_STATIONS_URL,
            content=_fixture_bytes("ogd-smn-precip_meta_stations.csv"),
        )
        aioclient_mock.get(
            precip_station_now_url(precip_station),
            content=_fixture_bytes(f"ogd-smn-precip_{precip_station}_t_now.csv"),
        )

    # SLF IMIS mocks (opt-in, ADR-0009): only registered when a station code is
    # given. The runtime coordinator polls the station's latest measurement and
    # the whole-network daily-snow row; the station list is a config-flow concern
    # (patched there), so it is not registered here.
    if imis_station is not None:
        aioclient_mock.get(
            measurements_url(imis_station),
            content=_fixture_bytes(
                f"slf-imis_{imis_station.lower()}_measurements.json"
            ),
        )
        aioclient_mock.get(
            DAILY_SNOW_URL,
            content=_fixture_bytes("slf-imis_daily-snow.json"),
        )

    # Pollen mocks (opt-in): only registered when a pollen station is given.
    if pollen_station is not None:
        aioclient_mock.get(
            META_POLLEN_DATAINVENTORY_URL,
            content=_fixture_bytes("ogd-pollen_meta_datainventory.csv"),
        )
        now_bytes = (
            pollen_now_content
            if pollen_now_content is not None
            else _fixture_bytes(f"ogd-pollen_{pollen_station}_h_now.csv")
        )
        aioclient_mock.get(
            pollen_now_url(pollen_station),
            content=now_bytes,
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


@pytest.fixture
def mock_ogd_precip(aioclient_mock):
    """Like ``mock_ogd`` but also mocks the BEL precipitation station (ADR-0006).

    Registers the precip meta-stations list and the BEL ``_t_now.csv`` fixture
    in addition to the full weather station + forecast mocks. BEL (Belp) is the
    nearest precipitation station to the Köniz forecast point.
    """
    _register_mock_ogd(aioclient_mock, precip_station="bel")
    return aioclient_mock


@pytest.fixture
def mock_ogd_imis(aioclient_mock):
    """Like ``mock_ogd`` but also mocks the STH2 IMIS snow station (ADR-0009).

    Registers the STH2 measurement endpoint and the whole-network daily-snow
    response in addition to the full weather station + forecast mocks. STH2
    (Vorderstocken) is one of the three nearest SNOW_FLAT stations to Köniz.
    """
    _register_mock_ogd(aioclient_mock, imis_station="STH2")
    return aioclient_mock


@pytest.fixture
def mock_ogd_pollen(aioclient_mock):
    """Like ``mock_ogd`` but also mocks the PBE pollen station (ADR-0005).

    Registers the pollen datainventory and the PBE ``_h_now.csv`` fixture in
    addition to the full weather station + forecast mocks.
    """
    _register_mock_ogd(aioclient_mock, pollen_station=_POLLEN_STATION_ABBR)
    return aioclient_mock


@pytest.fixture
def mock_ogd_pollen_empty(aioclient_mock):
    """Like ``mock_ogd_pollen`` but PBE's ``_h_now.csv`` has only empty rows.

    Used to test that pollen sensors become unavailable when the file carries
    no complete measurement rows.
    """
    empty_body = (
        b"station_abbr;reference_timestamp;kabetuh0;khpoach0;"
        b"kaalnuh0;kacoryh0;kafaguh0;kafraxh0;kaquerh0\n"
        b"PBE;28.08.2026 06:00;;;;;;;\n"
        b"PBE;28.08.2026 07:00;;;;;;;\n"
    )
    _register_mock_ogd(
        aioclient_mock,
        pollen_station=_POLLEN_STATION_ABBR,
        pollen_now_content=empty_body,
    )
    return aioclient_mock
