#!/usr/bin/env python3
"""Refresh the trimmed fixture files from the live MeteoSwiss open data.

Stdlib-only (urllib, csv, io, json, pathlib). Hits the network — run on a
machine with internet access when upstream schemas or content change. Writes
directly to ``tests/fixtures/``; review the diff before committing.

Usage::

    python tests/tools/refresh_fixtures.py
    python tests/tools/refresh_fixtures.py --zprfr0hs [RUN [LOCAL_FILE]]
"""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

OGD_BASE = "https://data.geo.admin.ch"
STAC_BASE = f"{OGD_BASE}/api/stac/v1"

COLLECTION_STATIONS = "ch.meteoschweiz.ogd-smn"
COLLECTION_FORECAST = "ch.meteoschweiz.ogd-local-forecasting"

META_STATIONS_URL = f"{OGD_BASE}/{COLLECTION_STATIONS}/ogd-smn_meta_stations.csv"
META_DATAINVENTORY_URL = (
    f"{OGD_BASE}/{COLLECTION_STATIONS}/ogd-smn_meta_datainventory.csv"
)
META_POINT_URL = (
    f"{OGD_BASE}/{COLLECTION_FORECAST}/ogd-local-forecasting_meta_point.csv"
)
STAC_ITEMS_URL = f"{STAC_BASE}/collections/{COLLECTION_FORECAST}/items"

# Stations to keep in the trimmed metadata fixture and data inventory.
KEEP_STATIONS = {"ABO", "BER", "RAG"}

# Forecast point primary keys (point_id, point_type_id) to keep.
KEEP_POINTS = {(1, 1), (309800, 2), (309801, 2), (5000, 3)}

# Station whose observation file is included (lowercase).
OBSERVATION_STATION = "ber"

# Number of daily forecast runs to keep in the STAC items fixture.
MAX_RUNS_IN_ITEMS = 2

# Daily parameter codes to download. The ``p``-variants are used, not the
# ``d``/``0``-variants: the ``d`` files are station-only, so a postal-code
# point would have no temperatures or precipitation (issue #34, docs/ogd.md
# §E4). ``jp2000d0`` (the symbol) already covers all point types.
DAILY_PARAMS = ("tre200px", "tre200pn", "rka150p0", "jp2000d0")

# Forecast points to keep in the daily CSV fixtures. Must include a postal-code
# centre (type 2) — the config-flow default — so the fixtures can catch the
# issue #34 regression, plus a station point (type 1) to prove both survive.
KEEP_DAILY_POINTS = {(1, 1), (309800, 2), (309801, 2)}

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


def _trim_csv_datainventory(raw: bytes) -> bytes:
    """Keep KEEP_STATIONS rows from the data inventory; preserve encoding."""
    text = raw.decode("cp1252")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    assert reader.fieldnames, "no header in datainventory CSV"
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
    """Keep only the KEEP_DAILY_POINTS rows; preserve encoding and order."""
    text = raw.decode("iso-8859-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    assert reader.fieldnames, f"no header in {param} CSV"
    rows = [
        r for r in reader
        if (int(r["point_id"]), int(r["point_type_id"])) in KEEP_DAILY_POINTS
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=reader.fieldnames, delimiter=";",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("iso-8859-1")


def _trim_date_major_rows(
    raw: bytes, *, keep_every: int, last_stamp: str, point: tuple[int, int]
) -> bytes:
    """Thin a date-major file: every ``keep_every``-th row of each block.

    Keeps the header, the upstream byte order, the real values (so the real
    value widths) and ``point``'s row in every block, for the blocks up to and
    including ``last_stamp``. Each block shrinks by about ``keep_every`` while
    its size still varies with the values, so a test scales its read windows
    by the same factor (issue #281).
    """
    lines = raw.split(b"\n")
    kept = [lines[0]]
    want = f"{point[0]};{point[1]};".encode("ascii")
    block, index = b"", 0
    for line in lines[1:]:
        parts = line.split(b";", 3)
        if len(parts) < 4:
            continue
        if parts[2] != block:
            block, index = parts[2], 0
        if parts[2].decode("ascii") > last_stamp:
            break
        if index % keep_every == 0 or line.startswith(want):
            kept.append(line)
        index += 1
    return b"\n".join(kept) + b"\n"


# The winter zprfr0hs fixture (issue #281): mixed 3-/4-digit values, so the
# hour blocks differ in size. Cut from run 202611251200 on request only, since
# the latest run may have no 3-digit values at all (it is seasonal).
ZPRFR0HS_RUN = "202611251200"
ZPRFR0HS_KEEP_EVERY = 16
ZPRFR0HS_LAST_STAMP = "202611262300"
ZPRFR0HS_POINT = (309800, 2)


def refresh_zprfr0hs(run_ts: str = ZPRFR0HS_RUN, source: Path | None = None) -> None:
    """Cut the thinned winter ``zprfr0hs`` fixture from ``run_ts``.

    ``source`` is a local copy of the full upstream file; without it the file
    is downloaded (~33 MB) while the run is still published.
    """
    name = f"vnut12.lssw.{run_ts}.zprfr0hs.csv"
    if source is not None:
        raw = source.read_bytes()
    else:
        raw = _get(
            f"{OGD_BASE}/{COLLECTION_FORECAST}/{run_ts[:8]}-ch/{name}"
        )
    (FIXTURES / f"vnut12.lssw.{run_ts}.zprfr0hs.thinned.csv").write_bytes(
        _trim_date_major_rows(
            raw,
            keep_every=ZPRFR0HS_KEEP_EVERY,
            last_stamp=ZPRFR0HS_LAST_STAMP,
            point=ZPRFR0HS_POINT,
        )
    )


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

    print("Fetching data inventory…")
    raw = _get(META_DATAINVENTORY_URL)
    (FIXTURES / "ogd-smn_meta_datainventory.csv").write_bytes(
        _trim_csv_datainventory(raw)
    )

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
    if len(sys.argv) > 1 and sys.argv[1] == "--zprfr0hs":
        # python tests/tools/refresh_fixtures.py --zprfr0hs [RUN [LOCAL_FILE]]
        args = sys.argv[2:]
        refresh_zprfr0hs(
            args[0] if args else ZPRFR0HS_RUN,
            Path(args[1]) if len(args) > 1 else None,
        )
    else:
        main()
