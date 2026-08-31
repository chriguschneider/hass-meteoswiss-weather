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
import importlib.util
import io
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

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

# The parameter codes the integration fetches are the tripwire's source of
# truth: a hardcoded expectation that drifted from the integration is exactly
# what issue #34 was. Load them from the integration's own const.py instead of
# repeating them here. That module imports only ``__future__`` (the ogd package
# stays pure — ADR-0001), so it is loaded in isolation: importing the package
# proper would pull in aiohttp, which this stdlib-only script must not need.
def _load_ogd_const():
    const_path = (
        Path(__file__).resolve().parents[2]
        / "custom_components"
        / "meteoswiss_weather"
        / "ogd"
        / "const.py"
    )
    spec = importlib.util.spec_from_file_location("_ogd_const", const_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load ogd const from {const_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_OGD_CONST = _load_ogd_const()
# Daily params the integration fetches for the default forecast (ADR-0002).
DAILY_PARAMS = set(_OGD_CONST.DAILY_REQUIRED_PARAMS)
# Hourly params the integration fetches for the opt-in hourly forecast.
HOURLY_PARAMS = set(_OGD_CONST.HOURLY_REQUIRED_PARAMS)

# Expected byte-order layout of each hourly file the integration reads (issue
# #50). The Range strategy in ogd/hourly.py detects this at runtime, but the
# smoke test pins it so an upstream re-sort is caught before it degrades the
# integration to full downloads. "date" = sorted by Date (horizon prefix works);
# "point" = one point's rows contiguous (block Range works).
HOURLY_EXPECTED_LAYOUT = {
    "tre200h0": "date",
    "rre150h0": "point",
    "jww003i0": "point",
    "fu3010h0": "point",
    "fu3010h1": "point",
    "dkl010h0": "point",
    # Cloud files are date-major (docs/ogd.md §E4, row-order table).
    "nprohihs": "date",
    "npromths": "date",
    "nprolohs": "date",
}

# Cloud parameters to sanity-check for value range (issue #97).
CLOUD_PARAMS = ("nprohihs", "npromths", "nprolohs")

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


def _range_get(url: str, start: int, end: int) -> tuple[bytes, int | None]:
    """Fetch bytes [start, end] (inclusive); return (body, total_size)."""
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        body = resp.read()
        cr = resp.headers.get("Content-Range")
    total = None
    if cr and "/" in cr:
        tail = cr.rsplit("/", 1)[1].strip()
        total = int(tail) if tail.isdigit() else None
    return body, total


def _row_after(url: str, offset: int, total: int) -> tuple[int, int, str] | None:
    """First complete data row after the newline at/after ``offset``.

    Returns (point_id, point_type_id, Date) or None. Rows are short, so a 2 KB
    window always spans a whole row and its bounding newlines.
    """
    if offset >= total:
        return None
    end = min(offset + 2048, total) - 1
    body, _ = _range_get(url, offset, end)
    nl = body.find(b"\n")
    if nl == -1:
        return None
    nl2 = body.find(b"\n", nl + 1)
    line = body[nl + 1 : nl2] if nl2 != -1 else body[nl + 1 :]
    parts = line.split(b";", 3)
    if len(parts) < 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), parts[2].decode("ascii", "ignore")
    except ValueError:
        return None


def _classify_layout_live(url: str) -> str:
    """Classify an hourly file as "date", "point" or "other" via offset probes."""
    _, total = _range_get(url, 0, 1023)
    if not total:
        return "other"
    rows = []
    for i in range(9):
        row = _row_after(url, total * i // 9, total)
        if row is not None and (not rows or row != rows[-1]):
            rows.append(row)
    if len(rows) < 3:
        return "other"
    dates = [r[2] for r in rows]
    type_keys = [(r[1], r[0]) for r in rows]
    ids = [r[0] for r in rows]

    def nondec(seq):
        return all(a <= b for a, b in zip(seq, seq[1:], strict=False))

    if nondec(dates) and len(set(dates)) > 1:
        return "date"
    if (nondec(type_keys) and len(set(type_keys)) > 1) or (
        nondec(ids) and len(set(ids)) > 1
    ):
        return "point"
    return "other"


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
    "Every daily file has the postal-code point "
    f"{FORECAST_POINT_ID};{FORECAST_POINT_TYPE_ID} (issue #34 tripwire)"
)


def _check_one_daily_file(param: str, href: str) -> str:
    """Return "" if the daily file for ``param`` is good, else why it failed.

    Good means the header is ``point_id;point_type_id;Date;<param>`` and the
    file carries at least one row for the default postal-code point. Issue #34
    was a daily file (the station-only ``d``/``0`` variants) that had every
    station row but no postal-code row, so the default point silently got no
    temperatures — hence checking the postal-code row in *every* daily file.
    """
    raw = _get(href)
    text = raw.decode("iso-8859-1")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    fields = set(reader.fieldnames or [])
    missing_cols = {"point_id", "point_type_id", "Date", param} - fields
    if missing_cols:
        return f"missing header cols {sorted(missing_cols)}"
    postal_row = any(
        r["point_id"] == FORECAST_POINT_ID
        and r["point_type_id"] == FORECAST_POINT_TYPE_ID
        for r in reader
    )
    if not postal_row:
        return f"no row for {FORECAST_POINT_ID};{FORECAST_POINT_TYPE_ID}"
    return ""


def check_daily_files(hrefs: dict[str, str]) -> None:
    """Check 2: every daily param file carries a postal-code row.

    The daily files are 0.2-1.3 MB each; downloading all four is cheap.
    """
    label = _DAILY_LABEL
    problems: list[str] = []
    for param in sorted(DAILY_PARAMS):
        href = hrefs.get(param)
        if not href:
            problems.append(f"{param}: no href in run")
            continue
        try:
            reason = _check_one_daily_file(param, href)
        except Exception as exc:  # noqa: BLE001 - report, do not abort other params
            reason = str(exc)
        if reason:
            problems.append(f"{param}: {reason}")
    _report(label, not problems, "; ".join(problems))


def check_hourly_layouts(hrefs: dict[str, str]) -> None:
    """Check: every hourly file the integration reads has its expected layout.

    The Range strategy (issue #50) depends on `tre200h0` being date-major and
    the symbol/precip/wind files being point-major; a re-sort upstream would
    silently degrade the integration to full downloads. Probing a few byte
    offsets per file is cheap (a handful of KB total).
    """
    label = "Hourly files have their expected Range layout (issue #50)"
    problems: list[str] = []
    for param, expected in sorted(HOURLY_EXPECTED_LAYOUT.items()):
        href = hrefs.get(param)
        if not href:
            problems.append(f"{param}: no href in run")
            continue
        try:
            got = _classify_layout_live(href)
        except Exception as exc:  # noqa: BLE001 - report, keep checking others
            got = f"error: {exc}"
        if got != expected:
            problems.append(f"{param}: expected {expected}, got {got}")
    _report(label, not problems, "; ".join(problems))


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


def _fetch_cloud_values(href: str) -> list[float]:
    """Fetch a small prefix of a date-major cloud file and extract the point's values.

    Date-major files start with the earliest hours for all points. The first
    200 KB covers several hours of all ~5,600 points, which is enough to extract
    several rows for our forecast point.
    """
    param = href.rsplit(".", 2)[-2]  # e.g. "nprohihs" from "...nprohihs.csv"
    body, _ = _range_get(href, 0, 200 * 1024 - 1)
    text = body.decode("iso-8859-1", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    vals: list[float] = []
    for row in reader:
        if (
            row.get("point_id") == FORECAST_POINT_ID
            and row.get("point_type_id") == FORECAST_POINT_TYPE_ID
        ):
            raw = row.get(param, "").strip()
            try:
                vals.append(float(raw))
            except ValueError:
                pass
    return vals


def check_cloud_value_range(hrefs: dict[str, str]) -> None:
    """Check: cloud-layer values for the forecast point are in [0, 100] after scaling.

    MeteoSwiss silently changed nprohihs/npromths/nprolohs from percent (0–100)
    to fraction (0–1) on 2026-08-31 (issue #97). The integration applies a
    per-file heuristic: if max ≤ 1.0 → fraction → ×100. This check fetches a
    small prefix of each cloud file, applies the same heuristic, and verifies
    the scaled values land in [0, 100]. It also reports which format was detected
    so a future format change is visible in the CI log.
    """
    label = "Cloud-layer values in [0, 100] after unit heuristic (issue #97)"
    problems: list[str] = []
    for param in CLOUD_PARAMS:
        href = hrefs.get(param)
        if not href:
            problems.append(f"{param}: no href in run")
            continue
        try:
            vals = _fetch_cloud_values(href)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{param}: fetch error: {exc}")
            continue
        if not vals:
            problems.append(
                f"{param}: no rows for {FORECAST_POINT_ID};{FORECAST_POINT_TYPE_ID}"
            )
            continue
        fmt = "fraction" if max(vals) <= 1.0 else "percent"
        scaled = [v * 100 if fmt == "fraction" else v for v in vals]
        out_of_range = [v for v in scaled if not 0.0 <= v <= 100.0]
        if out_of_range:
            problems.append(
                f"{param}: {len(out_of_range)} value(s) outside [0,100] "
                f"after {fmt} scaling: e.g. {out_of_range[0]}"
            )
        else:
            print(f"   {param}: {fmt} format, {len(vals)} rows, "
                  f"range [{min(scaled):.1f}, {max(scaled):.1f}]%")
    _report(label, not problems, "; ".join(problems))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("MeteoSwiss upstream smoke test")
    print("=" * 50)

    _run_ts, hrefs = check_stac()
    if hrefs:
        check_daily_files(hrefs)
        check_hourly_layouts(hrefs)
        check_cloud_value_range(hrefs)
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
