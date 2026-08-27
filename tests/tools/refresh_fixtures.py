#!/usr/bin/env python3
"""Refresh the trimmed fixture files from the live MeteoSwiss open data.

Stdlib-only (urllib, csv, io, json, pathlib). Hits the network — run on a
machine with internet access when upstream schemas or content change. Writes
directly to ``tests/fixtures/``; review the diff before committing.

Usage::

    python tests/tools/refresh_fixtures.py
"""

from __future__ import annotations

import csv
import io
import json
import urllib.request
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

OGD_BASE = "https://data.geo.admin.ch"
STAC_BASE = f"{OGD_BASE}/api/stac/v1"

COLLECTION_STATIONS = "ch.meteoschweiz.ogd-smn"
COLLECTION_FORECAST = "ch.meteoschweiz.ogd-local-forecasting"

META_STATIONS_URL = f"{OGD_BASE}/{COLLECTION_STATIONS}/ogd-smn_meta_stations.csv"
META_POINT_URL = (
    f"{OGD_BASE}/{COLLECTION_FORECAST}/ogd-local-forecasting_meta_point.csv"
)
STAC_ITEMS_URL = f"{STAC_BASE}/collections/{COLLECTION_FORECAST}/items"

# Stations to keep in the trimmed metadata fixture.
KEEP_STATIONS = {"ABO", "BER", "RAG"}

# Forecast point primary keys (point_id, point_type_id) to keep.
KEEP_POINTS = {(1, 1), (309800, 2), (309801, 2), (5000, 3)}

# Station whose observation file is included (lowercase).
OBSERVATION_STATION = "ber"

# Number of daily forecast runs to keep in the STAC items fixture.
MAX_RUNS_IN_ITEMS = 2

# Daily parameter codes to download.
DAILY_PARAMS = ("tre200dx", "tre200dn", "rka150d0", "jp2000d0")

# Forecast point to keep in the daily CSV fixtures.
KEEP_FORECAST_POINT = (309800, 2)

# Observation rows to keep (last N rows including header).
OBSERVATION_TAIL_ROWS = 3


def _get(url: str) -> bytes:
    print(f"  GET {url}")
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        return resp.read()


def _trim_csv_stations(raw: bytes) -> bytes:
    """Keep KEEP_STATIONS rows; preserve encoding and separator."""
    text = raw.decode("cp1252")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    assert reader.fieldnames, "no header in station CSV"
    rows = [r for r in reader if r["station_abbr"] in KEEP_STATIONS]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=reader.fieldnames, delimiter=";",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("cp1252")


def _trim_csv_observation(raw: bytes) -> bytes:
    """Keep the header and the last OBSERVATION_TAIL_ROWS data rows."""
    text = raw.decode("cp1252")
    lines = text.splitlines()
    header = lines[0]
    data = lines[1:]
    kept = [header, *data[-OBSERVATION_TAIL_ROWS:]]
    return ("\n".join(kept) + "\n").encode("cp1252")


def _trim_csv_points(raw: bytes) -> bytes:
    """Keep KEEP_POINTS rows; preserve encoding and separator."""
    text = raw.decode("iso-8859-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    assert reader.fieldnames, "no header in point CSV"
    rows = [
        r for r in reader
        if (int(r["point_id"]), int(r["point_type_id"])) in KEEP_POINTS
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=reader.fieldnames, delimiter=";",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("iso-8859-1")


def _trim_csv_daily(raw: bytes, param: str) -> bytes:
    """Keep only the KEEP_FORECAST_POINT rows; preserve encoding."""
    text = raw.decode("iso-8859-1")
    pid, ptid = KEEP_FORECAST_POINT
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    assert reader.fieldnames, f"no header in {param} CSV"
    rows = [
        r for r in reader
        if int(r["point_id"]) == pid and int(r["point_type_id"]) == ptid
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=reader.fieldnames, delimiter=";",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("iso-8859-1")


def _trim_stac_items(raw: bytes) -> bytes:
    """Keep MAX_RUNS_IN_ITEMS features; preserve JSON structure."""
    doc = json.loads(raw)
    doc["features"] = doc["features"][:MAX_RUNS_IN_ITEMS]
    # Drop pagination links that would point at non-existing pages.
    doc["links"] = [lk for lk in doc.get("links", []) if lk.get("rel") != "next"]
    return json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")


def _find_latest_complete_run(doc: dict) -> tuple[str, dict[str, str]]:
    """Return (run_ts, {param: href}) for the most recent complete run."""
    runs: dict[str, dict[str, str]] = {}
    for feat in doc.get("features", []):
        for asset_name, asset in feat.get("assets", {}).items():
            # Asset name: vnut12.lssw.YYYYMMDDHHMM.<param>.csv
            parts = asset_name.replace(".csv", "").split(".")
            if len(parts) < 4:
                continue
            run_ts = parts[2]
            param = parts[3]
            runs.setdefault(run_ts, {})[param] = asset["href"]

    required = set(DAILY_PARAMS)
    for ts in sorted(runs, reverse=True):
        if required.issubset(runs[ts]):
            return ts, runs[ts]
    raise RuntimeError("no complete run found in STAC items")


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)

    print("Fetching station metadata…")
    raw = _get(META_STATIONS_URL)
    (FIXTURES / "ogd-smn_meta_stations.csv").write_bytes(_trim_csv_stations(raw))

    abbr = OBSERVATION_STATION.lower()
    obs_url = f"{OGD_BASE}/{COLLECTION_STATIONS}/{abbr}/ogd-smn_{abbr}_t_now.csv"
    print(f"Fetching {abbr.upper()} observation file…")
    raw = _get(obs_url)
    (FIXTURES / f"ogd-smn_{abbr}_t_now.csv").write_bytes(_trim_csv_observation(raw))

    print("Fetching forecast point metadata…")
    raw = _get(META_POINT_URL)
    (FIXTURES / "ogd-local-forecasting_meta_point.csv").write_bytes(
        _trim_csv_points(raw)
    )

    print("Fetching STAC items listing…")
    raw = _get(STAC_ITEMS_URL)
    stac_doc = json.loads(raw)
    run_ts, hrefs = _find_latest_complete_run(stac_doc)
    print(f"  Latest complete run: {run_ts}")
    (FIXTURES / "ogd-local-forecasting_items.json").write_bytes(
        _trim_stac_items(raw)
    )

    for param in DAILY_PARAMS:
        href = hrefs[param]
        print(f"Fetching {param}…")
        raw = _get(href)
        out = f"vnut12.lssw.{run_ts}.{param}.csv"
        (FIXTURES / out).write_bytes(_trim_csv_daily(raw, param))
        # Remove fixtures for any other run timestamps.
        for old in FIXTURES.glob(f"vnut12.lssw.*.{param}.csv"):
            if old.name != out:
                print(f"  Removing stale {old.name}")
                old.unlink()

    print("Done. Review with: git diff tests/fixtures/")


if __name__ == "__main__":
    main()
