# The integration brand icon

Home Assistant shows the default puzzle-piece icon for an integration
until brand images exist for its domain. Since HA 2026.3 a custom
integration can carry them itself: the brands proxy API serves a `brand/`
folder from inside the integration and prefers it over the
[`home-assistant/brands`](https://github.com/home-assistant/brands) CDN —
see the [Brands Proxy API announcement](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api).
The legacy `custom_integrations/` route in the brands repository is closed.

```
custom_components/meteoswiss_weather/brand/
├── icon.png      # 256×256
└── icon@2x.png   # 512×512
```

## Decision

The icons are the **official MeteoSwiss app icon**, the same files and
the same maintainer decision as the sibling radar integration (ADR-0003:
one family, one icon). The reasoning — including the Coat of Arms
Protection Act consideration for the Swiss cross and the `meteo_france`
precedent in Home Assistant's own brand set — is recorded in the radar
repo's
[`docs/brands-icon.md`](https://github.com/chriguschneider/hass-meteoswiss-radar/blob/master/docs/brands-icon.md)
and is not repeated here.

## Regenerating

Only necessary when MeteoSwiss changes the app icon. Nothing calls this
automatically — it is not a build step.

```bash
pip install Pillow
python scripts/generate_brands_icon.py
```

`tests/test_brands_icon.py` guards the shipped files (presence, PNG
header, dimensions) and the generator's pure logic without Pillow or
network access.
