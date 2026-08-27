# MeteoSwiss forecast symbol codes → HA conditions

The MeteoSwiss local-forecast parameters `jp2000d0` (daily weather symbol)
and `jww003i0` (hourly weather symbol) carry integer icon codes.  Day codes
run from 1 to about 42; night codes are the day base plus 100 (101–142).

**Source:** Rudd-O/hamsclientfork (MIT) used as the starting reference;
cross-checked against the MeteoSwiss app icon set.  Descriptions come from
the MeteoSwiss developer resources and the OGD parameter metadata file
(`ogd-local-forecasting_meta_parameters.csv`).

**Night-code rule:** night codes 101–142 yield the same HA condition as
their day base (code − 100), *except* that any code whose day base maps to
`sunny` yields `clear-night` instead.  In this table that applies to codes
101 and 102 (both map to `clear-night`).

**Unknown codes:** `condition_for_symbol()` returns `None` and emits one
`DEBUG`-level log per unique unknown code.

---

## Day codes (1–42)

| Code | MeteoSwiss label (de) | MeteoSwiss label (en) | HA condition |
|-----:|----------------------|-----------------------|--------------|
| 1 | Sonnig | Sunny | `sunny` |
| 2 | Leicht bewölkt, sonnig | Slightly cloudy, sunny | `sunny` |
| 3 | Wechselnd bewölkt | Partly cloudy | `partlycloudy` |
| 4 | Stark bewölkt | Mostly cloudy | `cloudy` |
| 5 | Bedeckt | Overcast | `cloudy` |
| 6 | Bedeckt, etwas Regen | Overcast, some rain | `rainy` |
| 7 | Regen | Rain | `rainy` |
| 8 | Starker Regen | Heavy rain | `pouring` |
| 9 | Regen und Schnee / Schneeregen | Rain and snow / sleet | `snowy-rainy` |
| 10 | Schneefall | Snowfall | `snowy` |
| 11 | Leichter Schneefall | Light snowfall | `snowy` |
| 12 | Nebel | Fog | `fog` |
| 13 | Hochnebel | High fog / stratus | `fog` |
| 14 | Gewitter möglich | Thunderstorm possible | `lightning` |
| 15 | Gewitter | Thunderstorm | `lightning-rainy` |
| 16 | Gewitter mit Regen | Thunderstorm with rain | `lightning-rainy` |
| 17 | Gewitter mit Hagel | Thunderstorm with hail | `hail` |
| 18 | Hagel | Hail showers | `hail` |
| 19 | Sonnig, leichter Regen | Sunny, light rain | `rainy` |
| 20 | Sonnig, Regen | Sunny, rain | `rainy` |
| 21 | Wechselnd bewölkt, Schauer | Partly cloudy, showers | `rainy` |
| 22 | Wechselnd bewölkt, Gewitter | Partly cloudy, thunderstorm | `lightning-rainy` |
| 23 | Wechselnd bewölkt, Schneeschauer | Partly cloudy, snow showers | `snowy` |
| 24 | Bedeckt, leichter Regen | Overcast, light rain | `rainy` |
| 25 | Bedeckt, Gewitter | Overcast, thunderstorm | `lightning-rainy` |
| 26 | Bedeckt, Schneefall | Overcast, snowfall | `snowy` |
| 27 | Sonnig, Schauer | Sunny, showers | `rainy` |
| 28 | Sonnig, Gewitter mit Regen | Sunny, thunderstorm with rain | `lightning-rainy` |
| 29 | Sonnig, Schneeschauer | Sunny, snow showers | `snowy` |
| 30 | Sonnig, Graupelschauer | Sunny, sleet showers | `snowy-rainy` |
| 31 | Wechselnd bewölkt, Graupelschauer | Partly cloudy, sleet showers | `snowy-rainy` |
| 32 | Bedeckt, Graupelschauer | Overcast, sleet showers | `snowy-rainy` |
| 33 | Bedeckt, starker Schneefall | Overcast, heavy snowfall | `snowy` |
| 34 | Bedeckt, Regen | Overcast, rain | `rainy` |
| 35 | Bedeckt, starker Regen | Overcast, heavy rain | `pouring` |
| 36 | Sonnig, Gewitter mit Hagel | Sunny, thunderstorm with hail | `hail` |
| 37 | Wechselnd bewölkt, Gewitter mit Hagel | Partly cloudy, thunderstorm with hail | `hail` |
| 38 | Bedeckt, Gewitter mit Hagel | Overcast, thunderstorm with hail | `hail` |
| 39 | Nebel mit Niederschlag | Fog with precipitation | `rainy` |
| 40 | Hochnebel mit Niederschlag | High fog with precipitation | `rainy` |
| 41 | Hochnebel mit Schneefall | High fog with snowfall | `snowy` |
| 42 | Bedeckt, Gewitter | Overcast, thunderstorm | `lightning-rainy` |

## Night codes (101–142)

Night codes follow the rule above: same condition as the day base, but
`sunny` → `clear-night`.

| Code | Day base | HA condition |
|-----:|---------:|--------------|
| 101 | 1 (Sonnig) | `clear-night` |
| 102 | 2 (Leicht bewölkt, sonnig) | `clear-night` |
| 103–142 | 3–42 | same as day base |

The full night mapping is derived at runtime; see
`custom_components/meteoswiss_weather/symbols.py`.

---

> **Review note:** the German labels and the exact code-to-condition
> assignments should be verified against the OGD parameter metadata file
> (`ogd-local-forecasting_meta_parameters.csv`) and the MeteoSwiss app
> icon set before this table is considered authoritative.
