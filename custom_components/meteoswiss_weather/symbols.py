"""MeteoSwiss forecast symbol → Home Assistant condition mapping.

The MeteoSwiss local-forecast parameters ``jp2000d0`` (daily) and
``jww003i0`` (hourly) carry integer icon codes.  Day codes run from 1 to 42;
night codes run from 101 to 142 and are **not** a mechanical ``+100`` of the
day meaning — the icon set assigns them independently (e.g. ``26`` is *high
clouds* → ``sunny`` but ``126`` is *high cloud* at night → ``cloudy``), so
they are mapped from their own entries here.

The table is copied faithfully from ``CODE_TO_CONDITION_MAP`` in
`Rudd-O/homeassistant-meteoswiss <https://github.com/Rudd-O/homeassistant-meteoswiss/blob/master/custom_components/meteoswiss/const.py>`_
(MIT), which in turn dumps the official MeteoSwiss weather-icon spreadsheet
(``2022-02-14-Wetter-Icons-inkl-beschreibung``).  The English descriptions in
the trailing comments are that spreadsheet's wording.  See docs/symbols.md for
the full reviewable table.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

# Codes that have been logged at debug level so the log does not repeat on
# every forecast refresh for the same unknown code.
_unknown_logged: set[int] = set()

# MeteoSwiss icon code → HA ATTR_CONDITION_* string.  Day codes are 1–42,
# night codes 101–142; each is mapped independently (see module docstring).
# Comments are the official spreadsheet's English descriptions, kept verbatim
# so this table can be reviewed against the app icon set.
_CONDITIONS: dict[int, str] = {
    # --- day codes (1–42) ---------------------------------------------------
    1: "sunny",            # sunny
    2: "partlycloudy",     # mostly sunny, some clouds
    3: "partlycloudy",     # partly sunny, thick passing clouds
    4: "partlycloudy",     # overcast
    5: "cloudy",           # very cloudy
    6: "rainy",            # sunny intervals, isolated showers
    7: "snowy-rainy",      # sunny intervals, isolated sleet
    8: "snowy",            # sunny intervals, snow showers
    9: "rainy",            # overcast, some rain showers
    10: "snowy-rainy",     # overcast, some sleet
    11: "snowy",           # overcast, some snow showers
    12: "lightning",       # sunny intervals, chance of thunderstorms
    13: "lightning-rainy", # sunny intervals, possible thunderstorms
    14: "rainy",           # very cloudy, light rain
    15: "snowy-rainy",     # very cloudy, light sleet
    16: "snowy",           # very cloudy, light snow showers
    17: "rainy",           # very cloudy, intermittent rain
    18: "snowy-rainy",     # very cloudy, intermittent sleet
    19: "snowy",           # very cloudy, intermittent snow
    20: "pouring",         # very overcast with rain
    21: "snowy-rainy",     # very overcast with frequent sleet
    22: "snowy",           # very overcast with heavy snow
    23: "lightning-rainy", # very overcast, slight chance of storms
    24: "lightning-rainy", # very overcast with storms
    25: "lightning-rainy", # very cloudy, very stormy
    26: "sunny",           # high clouds
    27: "fog",             # stratus
    28: "fog",             # fog
    29: "rainy",           # sunny intervals, scattered showers
    30: "snowy",           # sunny intervals, scattered snow showers
    31: "snowy-rainy",     # sunny intervals, scattered sleet
    32: "lightning-rainy", # sunny intervals, some showers
    33: "rainy",           # short sunny intervals, frequent rain
    34: "snowy",           # short sunny intervals, frequent snowfalls
    35: "cloudy",          # overcast and dry
    36: "lightning",       # partly sunny, slightly stormy
    37: "snowy",           # partly sunny, stormy snow showers
    38: "lightning-rainy", # overcast, thundery showers
    39: "snowy-rainy",     # overcast, thundery snow showers
    40: "lightning",       # very cloudy, slightly stormy
    41: "lightning",       # overcast, slightly stormy
    42: "snowy",           # very cloudy, thundery snow showers
    # --- night codes (101–142) ----------------------------------------------
    101: "clear-night",     # clear
    102: "partlycloudy",    # slightly overcast
    103: "partlycloudy",    # heavy cloud formations
    104: "partlycloudy",    # overcast
    105: "cloudy",          # very cloudy
    106: "rainy",           # overcast, scattered showers
    107: "snowy-rainy",     # overcast, scattered rain and snow showers
    108: "snowy",           # overcast, snow showers
    109: "rainy",           # overcast, some showers
    110: "snowy-rainy",     # overcast, some rain and snow showers
    111: "snowy",           # overcast, some snow showers
    112: "lightning",       # slightly stormy
    113: "lightning-rainy", # storms
    114: "rainy",           # very cloudy, light rain
    115: "snowy-rainy",     # very cloudy, light rain and snow showers
    116: "snowy",           # very cloudy, light snowfall
    117: "rainy",           # very cloudy, intermittent rain
    118: "snowy-rainy",     # very cloudy, intermittent mixed rain and snowfall
    119: "snowy",           # very cloudy, intermittent snowfall
    120: "pouring",         # very cloudy, constant rain
    121: "snowy-rainy",     # very cloudy, frequent rain and snowfall
    122: "snowy",           # very cloudy, heavy snowfall
    123: "lightning-rainy", # very cloudy, slightly stormy
    124: "lightning-rainy", # very cloudy, stormy
    125: "lightning-rainy", # very cloudy, storms
    126: "cloudy",          # high cloud
    127: "fog",             # stratus
    128: "fog",             # fog
    129: "rainy",           # slightly overcast, scattered showers
    130: "snowy",           # slightly overcast, scattered snowfall
    131: "snowy-rainy",     # slightly overcast, rain and snow showers
    132: "lightning-rainy", # slightly overcast, some showers
    133: "rainy",           # overcast, frequent snow showers
    134: "snowy",           # overcast, frequent snow showers
    135: "cloudy",          # overcast and dry
    136: "lightning",       # slightly overcast, slightly stormy
    137: "snowy",           # slightly overcast, stormy snow showers
    138: "lightning-rainy", # overcast, thundery showers
    139: "snowy-rainy",     # overcast, thundery snow showers
    140: "lightning",       # very cloudy, slightly stormy
    141: "lightning",       # overcast, slightly stormy
    142: "snowy",           # very cloudy, thundery snow showers
}


def condition_for_symbol(
    code: int | None,
    *,
    is_daytime: bool | None = None,
) -> str | None:
    """Return the HA ``ATTR_CONDITION_*`` string for a MeteoSwiss symbol code.

    Day codes (1–42) and night codes (101–142) are looked up directly from
    their own table entries.  ``is_daytime`` says where the sun actually is
    and makes the code agree with it, in either direction:

    - ``False`` with a day code substitutes the night counterpart
      (``code + 100``) — the daily symbol rendered after sunset.
    - ``True`` with a night code substitutes the day counterpart
      (``code - 100``) — an hourly symbol whose night variant outlives
      sunrise (issue #103).

    Either way the substituted code is looked up on its own entry, so the icon
    set's independent night meaning is honoured (e.g. ``26`` → ``sunny`` by day
    but ``126`` → ``cloudy`` at night).  ``None`` leaves the code as sent.

    Returns ``None`` for ``None`` input or unknown codes; unknown codes are
    logged once per unique value at ``DEBUG`` level.
    """
    if code is None:
        return None

    # A daytime daily symbol shown at night takes its own night counterpart.
    if is_daytime is False and 1 <= code <= 42 and (code + 100) in _CONDITIONS:
        code += 100
    # ...and the mirror: a night symbol still in the feed after sunrise takes
    # its day counterpart (issue #103).
    elif is_daytime is True and 101 <= code <= 142 and (code - 100) in _CONDITIONS:
        code -= 100

    condition = _CONDITIONS.get(code)
    if condition is None:
        if code not in _unknown_logged:
            _LOGGER.debug("Unknown MeteoSwiss symbol code %d", code)
            _unknown_logged.add(code)
        return None
    return condition
