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
| `vnut12.lssw.202608270200.*.csv` | Daily parameter files, run 2026-08-27 02:00 UTC | Trimmed to point 309800 (9 rows) |

### Size budget

Keep each fixture under 50 KB. The daily parameter CSVs for the full
dataset are ~0.2 MB each; the trimmed versions are a few hundred bytes.
