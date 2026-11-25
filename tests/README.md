# Tests

## How to run

```
pip install -r requirements_test.txt
ruff check custom_components tests scripts
pytest -q
```

## Fixture files

All upstream responses are replayed from `tests/fixtures/` — the test suite
never hits the network. Fixtures are trimmed real files: they keep a handful
of rows/points, the real header, and the original encoding and separator
(Latin-1 / Windows-1252, `;`).

### How they were produced

The `tests/tools/refresh_fixtures.py` script fetches live files from
`data.geo.admin.ch` and trims them to the same small set of points/rows.
Run it on a machine with internet access when upstream schemas or content
change:

```
python tests/tools/refresh_fixtures.py
```

The script is stdlib-only (`urllib`, `csv`, `io`, `pathlib`) and writes
directly to `tests/fixtures/`. Review the diff before committing.

### Fixture inventory

| File | Source | Notes |
|---|---|---|
| `ogd-smn_meta_stations.csv` | `ch.meteoschweiz.ogd-smn/ogd-smn_meta_stations.csv` | Trimmed to ABO, BER, RAG |
| `ogd-smn_ber_t_now.csv` | `ch.meteoschweiz.ogd-smn/ber/ogd-smn_ber_t_now.csv` | Last 3 rows kept |
| `ogd-local-forecasting_meta_point.csv` | `ch.meteoschweiz.ogd-local-forecasting/ogd-local-forecasting_meta_point.csv` | Trimmed to points 1, 309800, 309801, 5000 |
| `ogd-local-forecasting_items.json` | STAC items listing for `ch.meteoschweiz.ogd-local-forecasting` | Trimmed to two runs (02:00 complete, 03:00 incomplete) |
| `vnut12.lssw.202608270200.{tre200px,tre200pn,rka150p0,jp2000d0}.csv` | Daily parameter files, run 2026-08-27 02:00 UTC | Trimmed to points 1, 309800, 309801 (27 rows). The `p`-variants are used, not the station-only `d`-variants (issue #34) |
| `vnut12.lssw.202608270200.{tre200h0,rre150h0,jww003i0,fu3010h0,fu3010h1,dkl010h0}.csv` | Hourly parameter files (opt-in), run 2026-08-27 02:00 UTC | Synthetic 24-hour rows for point 309800 |
| `vnut12.lssw.202611251200.zprfr0hs.thinned.csv` | `zprfr0hs`, run 2026-11-25 12:00 UTC (`20261125-ch` item) | Winter file with mixed 3-/4-digit values (issue #281). Blocks `202611242100`–`202611262300` (51 hour blocks), every 16th row of each block plus point 309800;2, upstream order and values unchanged. Cut with `python tests/tools/refresh_fixtures.py --zprfr0hs 202611251200 [LOCAL_FILE]` |

### Size budget

Keep each fixture under 50 KB. The daily parameter CSVs for the full
dataset are ~1.3 MB each (the `p`-variants cover every point type); the
trimmed versions are a few hundred bytes.

One exception: the thinned winter `zprfr0hs` (~480 KB). The row walk it tests
reads fixed byte windows (2 KB, 16 KB) around a predicted row, so the fixture
must keep realistic block sizes for 48+ hour blocks: with every 16th row a
block is ~9.6 KB and the test scales the windows by 1/16. Thinning further
would leave blocks smaller than a handful of rows and stop exercising the
drift that caused the issue.
