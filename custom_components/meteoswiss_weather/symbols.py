"""MeteoSwiss forecast symbol → Home Assistant condition mapping.

The MeteoSwiss local-forecast parameters ``jp2000d0`` (daily) and
``jww003i0`` (hourly) carry integer icon codes.  Day codes run from 1 to
about 42; night codes are the day base plus 100 (101–142).

Derived from Rudd-O/hamsclientfork (MIT) and cross-checked against the
MeteoSwiss app icon set; see docs/symbols.md for the full table.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

# Codes that have been logged at debug level so the log does not repeat on
# every forecast refresh for the same unknown code.
_unknown_logged: set[int] = set()

# Day-code (1–42) → HA ATTR_CONDITION_* string.
# Night codes (101–142) share this table via ``night_code - 100``.
# The ``clear-night`` override (for codes whose day equivalent is ``sunny``)
# is applied at call time; it is not stored here.
_DAY_CONDITIONS: dict[int, str] = {
    # --- clear to overcast --------------------------------------------------
    1: "sunny",           # Sonnig (Sunny)
    2: "sunny",           # Leicht bewölkt, sonnig (Slightly cloudy, sunny)
    3: "partlycloudy",    # Wechselnd bewölkt (Partly cloudy)
    4: "cloudy",          # Stark bewölkt (Mostly cloudy)
    5: "cloudy",          # Bedeckt (Overcast)
    # --- precipitation (no convection) --------------------------------------
    6: "rainy",           # Bedeckt, etwas Regen (Overcast, some rain)
    7: "rainy",           # Regen (Rain)
    8: "pouring",         # Starker Regen (Heavy rain)
    9: "snowy-rainy",     # Regen und Schnee / Schneeregen (Sleet)
    10: "snowy",          # Schneefall (Snowfall)
    11: "snowy",          # Leichter Schneefall (Light snowfall)
    # --- fog ----------------------------------------------------------------
    12: "fog",            # Nebel (Fog)
    13: "fog",            # Hochnebel (High fog / stratus)
    # --- convection ---------------------------------------------------------
    14: "lightning",      # Gewitter möglich (Thunderstorm possible)
    15: "lightning-rainy",# Gewitter (Thunderstorm)
    16: "lightning-rainy",# Gewitter mit Regen (Thunderstorm with rain)
    17: "hail",           # Gewitter mit Hagel (Thunderstorm with hail)
    18: "hail",           # Hagel (Hail showers)
    # --- mixed: sunny base --------------------------------------------------
    19: "rainy",          # Sonnig, leichter Regen (Sunny, light rain)
    20: "rainy",          # Sonnig, Regen (Sunny, rain)
    27: "rainy",          # Sonnig, Schauer (Sunny, showers)
    28: "lightning-rainy",# Sonnig, Gewitter mit Regen (Sunny, thunderstorm)
    29: "snowy",          # Sonnig, Schneeschauer (Sunny, snow showers)
    30: "snowy-rainy",    # Sonnig, Graupelschauer (Sunny, sleet showers)
    36: "hail",           # Sonnig, Gewitter mit Hagel (Sunny, thunderstorm + hail)
    # --- mixed: partly-cloudy base ------------------------------------------
    21: "rainy",          # Wechselnd bewölkt, Schauer (Partly cloudy, showers)
    22: "lightning-rainy",# Wechselnd bewölkt, Gewitter (Partly cloudy, thunderstorm)
    23: "snowy",          # Wechselnd bewölkt, Schneeschauer (Partly cloudy, snow)
    31: "snowy-rainy",    # Wechselnd bewölkt, Graupelschauer (Partly cloudy, sleet)
    37: "hail",           # Wechselnd bewölkt, Gewitter m. Hagel (Partly cloudy + hail)
    # --- mixed: overcast base -----------------------------------------------
    24: "rainy",          # Bedeckt, leichter Regen (Overcast, light rain)
    25: "lightning-rainy",# Bedeckt, Gewitter (Overcast, thunderstorm)
    26: "snowy",          # Bedeckt, Schneefall (Overcast, snowfall)
    32: "snowy-rainy",    # Bedeckt, Graupelschauer (Overcast, sleet)
    33: "snowy",          # Bedeckt, starker Schneefall (Overcast, heavy snow)
    34: "rainy",          # Bedeckt, Regen (Overcast, rain)
    35: "pouring",        # Bedeckt, starker Regen (Overcast, heavy rain)
    38: "hail",           # Bedeckt, Gewitter mit Hagel (Overcast, thunderstorm + hail)
    42: "lightning-rainy",# Bedeckt, Gewitter (Overcast, thunderstorm, no rain icon)
    # --- fog with precipitation ---------------------------------------------
    39: "rainy",          # Nebel mit Niederschlag (Fog with precipitation)
    40: "rainy",          # Hochnebel mit Niederschlag (High fog with precipitation)
    41: "snowy",          # Hochnebel mit Schneefall (High fog with snowfall)
}


def condition_for_symbol(
    code: int | None,
    *,
    is_daytime: bool | None = None,
) -> str | None:
    """Return the HA ``ATTR_CONDITION_*`` string for a MeteoSwiss symbol code.

    Night codes (101–142) yield the same condition as the day base
    (``code - 100``) except that any code whose day base maps to ``sunny``
    yields ``clear-night`` instead.  A day code (1–42) with
    ``is_daytime=False`` gets the same ``clear-night`` override.

    Returns ``None`` for ``None`` input or unknown codes; unknown codes are
    logged once per unique value at ``DEBUG`` level.
    """
    if code is None:
        return None

    night = 101 <= code <= 199
    day_code = (code - 100) if night else code

    condition = _DAY_CONDITIONS.get(day_code)
    if condition is None:
        if code not in _unknown_logged:
            _LOGGER.debug("Unknown MeteoSwiss symbol code %d", code)
            _unknown_logged.add(code)
        return None

    # Return the night variant when the code itself is a night code, or when
    # the caller signals nighttime for a day code.
    if (night or is_daytime is False) and condition == "sunny":
        return "clear-night"
    return condition
