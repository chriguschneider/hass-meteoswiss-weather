#!/usr/bin/env python3
"""Upstream smoke test for MeteoSwiss open data.

Stdlib only (urllib, csv, json). Hits the live endpoints — run on a machine
with internet access or via the weekly smoke-test workflow.

Exit 1 if any check fails; each check prints a mark and description.

The workflow sets PYTHONIOENCODING=utf-8; non-ASCII marks are printed only
through a TextIOWrapper configured explicitly so the script is safe locally
too.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
from datetime import datetime

# Reconfigure stdout for non-ASCII output regardless of the terminal locale.
sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
)

OGD_BASE = "https://data.geo.admin.ch"
STAC_BASE = f"{OGD_BASE}/api/stac/v1"

COLLECTION_STATIONS = "ch.meteoschweiz.ogd-smn"
COLLECTION_FORECAST = "ch.meteoschweiz.ogd-local-forecasting"

STAC_ITEMS_URL = f"{STAC_BASE}/collections/{COLLECTION_FORECAST}/items"
META_POINT_URL = (
    f"{OGD_BASE}/{COLLECTION_FORECAST}/ogd-local-forecasting_meta_point.csv"
)
META_STATIONS_URL = (
    f"{OGD_BASE}/{COLLECTION_STATIONS}/ogd-smn_meta_stations.csv"
)
BER_OBS_URL = (
    f"{OGD_BASE}/{COLLECTION_STATIONS}/ber/ogd-smn_ber_t_now.csv"
)

# Daily params expected in the newest forecast run (ADR-0002).
DAILY_PARAMS = {"tre200dx", "tre200dn", "rka150d0", "jp2000d0"}
# Hourly params expected in the newest forecast run (ADR-0002).
HOURLY_PARAMS = {"tre200h0", "rre150h0", "jww003i0", "fu3010h0"}

# Forecast point checked in the data files (postal code 3098 Köniz, n=00).
FORECAST_POINT_ID = "309800"
FORECAST_POINT_TYPE_ID = "2"

# Required columns in ogd-local-forecasting_meta_point.csv (docs/ogd.md E4).
POINT_META_REQUIRED = {
    "point_id",
    "point_type_id",
    "station_abbr",
    "postal_code",
    "point_name",
    "point_type_de",
    "point_type_fr",
    "point_type_it",
    "point_type_en",
    "point_height_masl",
    "point_coordinates_lv95_east",
    "point_coordinates_lv95_north",
    "point_coordinates_wgs84_lat",
    "point_coordinates_wgs84_lon",
}

# Required columns in ogd-smn_meta_stations.csv (docs/ogd.md A1; wildcard
# columns like station_exposition_* and station_url_* are excluded here).
STATION_META_REQUIRED = {
    "station_abbr",
    "station_name",
    "station_canton",
    "station_wigos_id",
    "station_dataowner",
    "station_data_since",
    "station_height_masl",
    "station_height_barometer_masl",
    "station_coordinates_lv95_east",
    "station_coordinates_lv95_north",
    "station_coordinates_wgs84_lat",
    "station_coordinates_wgs84_lon",
}

# Parameter codes the integration reads from the station 10-minute file
# (docs/ogd.md A1 table).
BER_REQUIRED_CODES = {
    "tre200s0",
    "ure200s0",
    "tde200s0",
    "prestas0",
    "pp0qffs0",
    "pp0qnhs0",
    "dkl010z0",
    "fu3010z0",
    "fu3010z1",
    "fkl010z0",
    "fkl010z1",
    "rre150z0",
    "sre000z0",
    "gre000z0",
    "tso005s0",
}

_failures: list[str] = []


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        return resp.read()


def _report(label: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    line = f"{mark}  {label}"
    if detail:
        line += f": {detail}"
    print(line)
    if not ok:
        _failures.append(label)


def _find_latest_complete_run(
    doc: dict,
) -> tuple[str, dict[str, str]]:
    """Return (run_ts, {param: href}) for the newest run that has all needed params."""
    runs: dict[str, dict[str, str]] = {}
    for feat in doc.get("features", []):
        for asset_name, asset in feat.get("assets", {}).items():
            # Asset names: vnut12.lssw.YYYYMMDDHHMM.<param>.csv
            parts = asset_name.replace(".csv", "").split(".")
            if len(parts) < 4:
                continue
            run_ts = parts[2]
            param = parts[3]
            runs.setdefault(run_ts, {})[param] = asset["href"]

    required = DAILY_PARAMS | HOURLY_PARAMS
    for ts in sorted(runs, reverse=True):
        if required.issubset(runs[ts]):
            return ts, runs[ts]
    raise RuntimeError(
        f"no run with all required params; need {sorted(required)}"
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_stac() -> tuple[str, dict[str, str]]:
    """Check 1: STAC items reachable and newest run has all required params."""
    label = "STAC items reachable, newest run has daily+hourly params"
    try:
        raw = _get(STAC_ITEMS_URL)
        doc = json.loads(raw)
        run_ts, hrefs = _find_latest_complete_run(doc)
        _report(label, True, f"run {run_ts}")
        return run_ts, hrefs
    except Exception as exc:
        _report(label, False, str(exc))
        return "", {}


_DAILY_LABEL = (
    "Daily file header point_id;point_type_id;Date;<param>"
    f" + row {FORECAST_POINT_ID};{FORECAST_POINT_TYPE_ID}"
)


def check_daily_file(hrefs: dict[str, str]) -> None:
    """Check 2: A daily file has expected header and a row for 309800;2."""
    label = _DAILY_LABEL
    param = "tre200dx"
    href = hrefs.get(param)
    if not href:
        _report(label, False, f"no href for {param}")
        return
    try:
        raw = _get(href)
        text = raw.decode("iso-8859-1")
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        fields = set(reader.fieldnames or [])
        required_cols = {"point_id", "point_type_id", "Date", param}
        missing_cols = required_cols - fields
        header_ok = not missing_cols
        row_found = any(
            r["point_id"] == FORECAST_POINT_ID
            and r["point_type_id"] == FORECAST_POINT_TYPE_ID
            for r in reader
        )
        parts: list[str] = []
        if missing_cols:
            parts.append(f"missing header cols: {sorted(missing_cols)}")
        if not row_found:
            parts.append(
                f"no row for {FORECAST_POINT_ID};{FORECAST_POINT_TYPE_ID}"
            )
        _report(label, header_ok and row_found, "; ".join(parts))
    except Exception as exc:
        _report(label, False, str(exc))


def check_point_meta() -> None:
    """Check 3: Point meta header matches docs/ogd.md and contains 309800."""
    label = f"Point meta header matches + contains {FORECAST_POINT_ID}"
    try:
        raw = _get(META_POINT_URL)
        text = raw.decode("iso-8859-1")
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        fields = set(reader.fieldnames or [])
        missing_cols = POINT_META_REQUIRED - fields
        header_ok = not missing_cols
        point_found = any(r["point_id"] == FORECAST_POINT_ID for r in reader)
        parts: list[str] = []
        if missing_cols:
            parts.append(f"missing header cols: {sorted(missing_cols)}")
        if not point_found:
            parts.append(f"point {FORECAST_POINT_ID} not found")
        _report(label, header_ok and point_found, "; ".join(parts))
    except Exception as exc:
        _report(label, False, str(exc))


def check_ber_obs() -> None:
    """Check 4: BER t_now header has required codes; last row timestamp parses."""
    label = "BER t_now header has required codes + timestamp dd.mm.yyyy HH:MM"
    try:
        raw = _get(BER_OBS_URL)
        text = raw.decode("cp1252")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            _report(label, False, "empty file")
            return
        header_fields = set(lines[0].split(";"))
        missing_codes = BER_REQUIRED_CODES - header_fields
        header_ok = not missing_codes
        parts: list[str] = []
        if missing_codes:
            parts.append(f"missing codes: {sorted(missing_codes)}")
        ts_ok = False
        if len(lines) >= 2:
            last_parts = lines[-1].split(";")
            if len(last_parts) >= 2:
                ts_str = last_parts[1]
                try:
                    datetime.strptime(ts_str, "%d.%m.%Y %H:%M")
                    ts_ok = True
                except ValueError:
                    parts.append(
                        f"timestamp '{ts_str}' does not match dd.mm.yyyy HH:MM"
                    )
            else:
                parts.append("last row has fewer than 2 fields")
        else:
            parts.append("no data rows")
        _report(label, header_ok and ts_ok, "; ".join(parts))
    except Exception as exc:
        _report(label, False, str(exc))


def check_station_meta() -> None:
    """Check 5: Station meta header matches docs/ogd.md."""
    label = "Station meta header matches"
    try:
        raw = _get(META_STATIONS_URL)
        text = raw.decode("cp1252")
        reader = csv.DictReader(io.StringIO(text), delimiter=";")
        fields = set(reader.fieldnames or [])
        missing_cols = STATION_META_REQUIRED - fields
        ok = not missing_cols
        detail = f"missing: {sorted(missing_cols)}" if missing_cols else ""
        _report(label, ok, detail)
    except Exception as exc:
        _report(label, False, str(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("MeteoSwiss upstream smoke test")
    print("=" * 50)

    _run_ts, hrefs = check_stac()
    if hrefs:
        check_daily_file(hrefs)
    else:
        _report(_DAILY_LABEL, False, "skipped — STAC check failed")

    check_point_meta()
    check_ber_obs()
    check_station_meta()

    print("=" * 50)
    if _failures:
        print(f"FAILED — {len(_failures)} check(s): {', '.join(_failures)}")
        sys.exit(1)
    else:
        print("All checks passed.")


if __name__ == "__main__":
    main()
