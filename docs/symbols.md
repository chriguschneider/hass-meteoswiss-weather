# MeteoSwiss forecast symbol codes → HA conditions

The MeteoSwiss local-forecast parameters `jp2000d0` (daily weather symbol)
and `jww003i0` (hourly weather symbol) carry integer icon codes.  Day codes
run from 1 to 42; night codes run from 101 to 142.

**Source:** the table is copied faithfully from `CODE_TO_CONDITION_MAP` in
[`Rudd-O/homeassistant-meteoswiss`](https://github.com/Rudd-O/homeassistant-meteoswiss/blob/master/custom_components/meteoswiss/const.py)
(MIT, in production for three years), which dumps the official MeteoSwiss
weather-icon spreadsheet
(`2022-02-14-Wetter-Icons-inkl-beschreibung-v1-an-website.xlsx`).  The English
descriptions below are that spreadsheet's wording.

**Night codes are independent, not day + 100.** The icon set assigns
101–142 their own meanings, so the mapping does **not** derive a night code
from `code − 100`.  The clearest example: day code `26` is *high clouds* →
`sunny`, but night code `126` is *high cloud* → `cloudy`; day `1` is `sunny`
while night `101` is `clear-night`.  Each code is mapped from its own entry.

**`is_daytime` hint:** the hint says where the sun actually is (`sun.sun`) and
makes the code agree with it, in either direction.  A *daily* symbol (a day
code 1–42) shown at night is substituted by its night counterpart
`code + 100`; a *night* code 101–142 rendered while the sun is up is
substituted by its day counterpart `code − 100`.  Either way the substituted
code is looked up on its own entry, so the night meaning above is honoured.

The mirror direction matters for the hourly symbol (`jww003i0`): it carries
its own day/night variant, but MeteoSwiss keeps sending the night variant for
a couple of hours past sunrise, which used to leave the entity on
`clear-night` in broad daylight (#103).  The per-hour conditions in the
*hourly forecast* are not corrected — those are future hours, and `sun.sun` is
a now-only flag.

**Unknown codes:** `condition_for_symbol()` returns `None` and emits one
`DEBUG`-level log per unique unknown code.

---

## Day codes (1–42)

| Code | MeteoSwiss meaning (en) | HA condition |
|-----:|-------------------------|--------------|
| 1 | sunny | `sunny` |
| 2 | mostly sunny, some clouds | `partlycloudy` |
| 3 | partly sunny, thick passing clouds | `partlycloudy` |
| 4 | overcast | `partlycloudy` |
| 5 | very cloudy | `cloudy` |
| 6 | sunny intervals, isolated showers | `rainy` |
| 7 | sunny intervals, isolated sleet | `snowy-rainy` |
| 8 | sunny intervals, snow showers | `snowy` |
| 9 | overcast, some rain showers | `rainy` |
| 10 | overcast, some sleet | `snowy-rainy` |
| 11 | overcast, some snow showers | `snowy` |
| 12 | sunny intervals, chance of thunderstorms | `lightning` |
| 13 | sunny intervals, possible thunderstorms | `lightning-rainy` |
| 14 | very cloudy, light rain | `rainy` |
| 15 | very cloudy, light sleet | `snowy-rainy` |
| 16 | very cloudy, light snow showers | `snowy` |
| 17 | very cloudy, intermittent rain | `rainy` |
| 18 | very cloudy, intermittent sleet | `snowy-rainy` |
| 19 | very cloudy, intermittent snow | `snowy` |
| 20 | very overcast with rain | `pouring` |
| 21 | very overcast with frequent sleet | `snowy-rainy` |
| 22 | very overcast with heavy snow | `snowy` |
| 23 | very overcast, slight chance of storms | `lightning-rainy` |
| 24 | very overcast with storms | `lightning-rainy` |
| 25 | very cloudy, very stormy | `lightning-rainy` |
| 26 | high clouds | `sunny` |
| 27 | stratus | `fog` |
| 28 | fog | `fog` |
| 29 | sunny intervals, scattered showers | `rainy` |
| 30 | sunny intervals, scattered snow showers | `snowy` |
| 31 | sunny intervals, scattered sleet | `snowy-rainy` |
| 32 | sunny intervals, some showers | `lightning-rainy` |
| 33 | short sunny intervals, frequent rain | `rainy` |
| 34 | short sunny intervals, frequent snowfalls | `snowy` |
| 35 | overcast and dry | `cloudy` |
| 36 | partly sunny, slightly stormy | `lightning` |
| 37 | partly sunny, stormy snow showers | `snowy` |
| 38 | overcast, thundery showers | `lightning-rainy` |
| 39 | overcast, thundery snow showers | `snowy-rainy` |
| 40 | very cloudy, slightly stormy | `lightning` |
| 41 | overcast, slightly stormy | `lightning` |
| 42 | very cloudy, thundery snow showers | `snowy` |

## Night codes (101–142)

| Code | MeteoSwiss meaning (en) | HA condition |
|-----:|-------------------------|--------------|
| 101 | clear | `clear-night` |
| 102 | slightly overcast | `partlycloudy` |
| 103 | heavy cloud formations | `partlycloudy` |
| 104 | overcast | `partlycloudy` |
| 105 | very cloudy | `cloudy` |
| 106 | overcast, scattered showers | `rainy` |
| 107 | overcast, scattered rain and snow showers | `snowy-rainy` |
| 108 | overcast, snow showers | `snowy` |
| 109 | overcast, some showers | `rainy` |
| 110 | overcast, some rain and snow showers | `snowy-rainy` |
| 111 | overcast, some snow showers | `snowy` |
| 112 | slightly stormy | `lightning` |
| 113 | storms | `lightning-rainy` |
| 114 | very cloudy, light rain | `rainy` |
| 115 | very cloudy, light rain and snow showers | `snowy-rainy` |
| 116 | very cloudy, light snowfall | `snowy` |
| 117 | very cloudy, intermittent rain | `rainy` |
| 118 | very cloudy, intermittent mixed rain and snowfall | `snowy-rainy` |
| 119 | very cloudy, intermittent snowfall | `snowy` |
| 120 | very cloudy, constant rain | `pouring` |
| 121 | very cloudy, frequent rain and snowfall | `snowy-rainy` |
| 122 | very cloudy, heavy snowfall | `snowy` |
| 123 | very cloudy, slightly stormy | `lightning-rainy` |
| 124 | very cloudy, stormy | `lightning-rainy` |
| 125 | very cloudy, storms | `lightning-rainy` |
| 126 | high cloud | `cloudy` |
| 127 | stratus | `fog` |
| 128 | fog | `fog` |
| 129 | slightly overcast, scattered showers | `rainy` |
| 130 | slightly overcast, scattered snowfall | `snowy` |
| 131 | slightly overcast, rain and snow showers | `snowy-rainy` |
| 132 | slightly overcast, some showers | `lightning-rainy` |
| 133 | overcast, frequent snow showers | `rainy` |
| 134 | overcast, frequent snow showers | `snowy` |
| 135 | overcast and dry | `cloudy` |
| 136 | slightly overcast, slightly stormy | `lightning` |
| 137 | slightly overcast, stormy snow showers | `snowy` |
| 138 | overcast, thundery showers | `lightning-rainy` |
| 139 | overcast, thundery snow showers | `snowy-rainy` |
| 140 | very cloudy, slightly stormy | `lightning` |
| 141 | overcast, slightly stormy | `lightning` |
| 142 | very cloudy, thundery snow showers | `snowy` |

---

> Descriptions and code→condition assignments are the reference spreadsheet's.
> MeteoSwiss models more cloud/rain gradations than Home Assistant does, so
> several codes collapse onto the same HA condition (e.g. 2/3/4 →
> `partlycloudy`).  Verify against the app icon set before changing an entry.
