"""Guard the settings-map table in docs/CONFIGURATION.md.

Parses the "What each setting controls" section and fails when a sensor key
from sensor.py or an extra hourly forecast key from weather.py is not mentioned
anywhere in that section.  This ensures the map cannot silently rot when a new
sensor or forecast field is added.

Keys are identified by backtick spans (``key``) so the table must name each
entity or forecast field by its real code-level identifier.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIGURATION_MD = ROOT / "docs" / "CONFIGURATION.md"
_SECTION_HEADING = "## What each setting controls"

# ---------------------------------------------------------------------------
# Expected keys — derived from the code, not from memory.
# ---------------------------------------------------------------------------

# Sensor keys from sensor.py: _SENSORS (station), _FORECAST_SENSORS (daily
# forecast aggregates), plus the four standalone sensor descriptions.
_SENSOR_KEYS: frozenset[str] = frozenset({
    # _SENSORS — station sensors
    "temperature",
    "humidity",
    "pressure_qff",
    "wind_speed",
    "wind_bearing",
    "gust_speed",
    "precipitation",
    "dew_point",
    "pressure_qfe",
    "pressure_qnh",
    "sunshine_duration",
    "global_radiation",
    "diffuse_radiation",
    "longwave_radiation",
    "snow_depth",
    "wind_chill",
    "air_temp_5cm",
    "soil_temp_5cm",
    "soil_temp_10cm",
    "soil_temp_20cm",
    # _FORECAST_SENSORS — today's daily forecast aggregates
    "temp_max_today",
    "temp_min_today",
    "precipitation_today",
    # Standalone sensor descriptions
    "zero_degree_level",
    "measurement_time",
    "data_fetched_today",
    "requests_today",
    # _POLLEN_SENSORS
    "pollen_grasses",
    "pollen_birch",
    "pollen_alder",
    "pollen_hazel",
    "pollen_beech",
    "pollen_ash",
    "pollen_oak",
})

# Extra (non-standard HA) forecast field keys added by weather.py
# _as_hourly_forecast — standard keys (condition, temperature, …) are
# already covered by the HA docs; only the integration-specific ones need
# to be guarded here.
_HOURLY_EXTRA_KEYS: frozenset[str] = frozenset({
    "radiation",
    "zero_degree_level",
    "cloud_coverage",
    "cloud_coverage_high",
    "cloud_coverage_mid",
    "cloud_coverage_low",
    "temperature_p10",
    "temperature_p90",
})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_section() -> str:
    """Return the body of the 'What each setting controls' section."""
    text = CONFIGURATION_MD.read_text(encoding="utf-8")
    match = re.search(
        rf"^{re.escape(_SECTION_HEADING)}\n(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, (
        f"docs/CONFIGURATION.md has no '{_SECTION_HEADING}' section. "
        "Add the settings-map table (issue #147)."
    )
    return match.group(1)


def _backtick_keys(section: str) -> set[str]:
    """Identifiers wrapped in backticks anywhere in the section."""
    return set(re.findall(r"`([^`\n]+)`", section))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_settings_map_section_exists() -> None:
    _load_section()


def test_all_sensor_keys_in_settings_map() -> None:
    section = _load_section()
    keys_in_doc = _backtick_keys(section)
    missing = _SENSOR_KEYS - keys_in_doc
    assert not missing, (
        "Settings map in docs/CONFIGURATION.md does not mention these sensor "
        f"keys: {sorted(missing)}. "
        "Add a row (or update an existing one) so the table covers every key."
    )


def test_all_hourly_extra_keys_in_settings_map() -> None:
    section = _load_section()
    keys_in_doc = _backtick_keys(section)
    missing = _HOURLY_EXTRA_KEYS - keys_in_doc
    assert not missing, (
        "Settings map in docs/CONFIGURATION.md does not mention these extra "
        f"hourly forecast keys: {sorted(missing)}. "
        "Add a row (or update an existing one) so the table covers every key."
    )
