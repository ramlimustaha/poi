#!/usr/bin/env python3
"""Download Points of Interest (POI) from OpenStreetMap for an Indonesian province.

Example:
    python download_poi.py --province "Jawa Barat" --category airport hospital
    python download_poi.py --province "Bali" --tag amenity=school shop=supermarket
"""
import argparse
import csv
import difflib
import hashlib
import json
import logging
import re
import sys
import threading
import time
from datetime import date
from pathlib import Path

import requests

import config

log = logging.getLogger("poi")

CSV_FIELDS = [
    "osm_id", "osm_type", "name", "category", "osm_tags", "province", "country",
    "latitude", "longitude", "source", "osm_url", "download_date",
]


SHOW_SPINNER = True  # the web app sets this to False


class POIError(Exception):
    """Recoverable, user-facing error."""


# --------------------------------------------------------------------------- utils
def setup_logging(verbose=False):
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(config.LOG_DIR / "poi_downloader.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(fh)
    except OSError as exc:  # read-only filesystem: log to console only
        print(f"File logging disabled: {exc}", file=sys.stderr)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(sh)


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def normalize_name(text):
    text = text.lower()
    text = re.sub(r"\b(provinsi|prov|propinsi|daerah|khusus|ibukota|special|region|province|of)\b\.?", " ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).split()


class Spinner:
    """Shows elapsed time while a long request is in progress."""

    def __init__(self, label, callback=None):
        """callback(seconds_elapsed) is called every second (used by the web app for progress)."""
        self.label, self._stop, self.callback = label, threading.Event(), callback
        self._console = callback is None and SHOW_SPINNER and sys.stdout.isatty()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        start = time.time()
        while not self._stop.wait(1):
            secs = int(time.time() - start)
            if self.callback:
                self.callback(secs)
            else:
                sys.stdout.write(f"\r  {self.label} ... {secs}s")
                sys.stdout.flush()

    def __enter__(self):
        if self.callback or self._console:
            self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._t.is_alive():
            self._t.join()
            if self._console:
                sys.stdout.write("\r" + " " * 70 + "\r")


_last_nominatim = 0.0


def http_request(method, url, *, timeout, **kwargs):
    """HTTP call with retry, backoff and status handling. Returns Response."""
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", config.USER_AGENT)
    last = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
            if resp.status_code in (429, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code} from {url}", response=resp)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last = exc
            log.warning("Attempt %d/%d failed for %s: %s", attempt, config.MAX_RETRIES, url, exc)
            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_BACKOFF * attempt)
    raise POIError(f"Request to {url} failed after {config.MAX_RETRIES} attempts: {last}")


# --------------------------------------------------------------------------- boundary
def _score_candidate(cand, province, country):
    """Return score >0 if candidate looks like the requested province, else 0."""
    if cand.get("osm_type") != "relation":
        return 0
    # jsonv2 uses "category", older format uses "class"
    if (cand.get("category") or cand.get("class")) != "boundary" or cand.get("type") != "administrative":
        return 0
    addr_country = (cand.get("address") or {}).get("country", "")
    cc = (cand.get("address") or {}).get("country_code", "")
    if addr_country and country.lower() not in (addr_country.lower(), cc.lower()) \
            and not difflib.get_close_matches(country.lower(), [addr_country.lower()], cutoff=0.8):
        return 0
    want = normalize_name(province)
    names = list((cand.get("namedetails") or {}).values()) + [cand.get("name", "")]
    best = 0.0
    for n in names:
        have = normalize_name(n)
        if not have:
            continue
        if have == want:
            best = max(best, 1.0)
        elif set(want) <= set(have) or set(have) <= set(want):
            best = max(best, 0.9)
        else:
            best = max(best, difflib.SequenceMatcher(None, " ".join(want), " ".join(have)).ratio() * 0.85)
    if best < 0.8:
        return 0
    level = str((cand.get("extratags") or {}).get("admin_level", ""))
    return best + (0.5 if level == "4" else 0)


def get_province_boundary(province, country):
    """Find the province administrative boundary via Nominatim (cached on disk)."""
    global _last_nominatim
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{province.lower()}|{country.lower()}".encode()).hexdigest()
    cache_file = config.CACHE_DIR / f"boundary_{slugify(province)}_{key[:8]}.json"
    if cache_file.exists():
        log.info("Using cached boundary: %s", cache_file.name)
        return json.loads(cache_file.read_text(encoding="utf-8"))

    wait = config.NOMINATIM_MIN_INTERVAL - (time.time() - _last_nominatim)
    if wait > 0:
        time.sleep(wait)
    params = {
        "q": f"{province}, {country}", "format": "jsonv2", "polygon_geojson": 1,
        "addressdetails": 1, "extratags": 1, "namedetails": 1, "limit": 10,
        "polygon_threshold": 0.001,
    }
    log.info("Searching boundary for '%s, %s' via Nominatim ...", province, country)
    resp = http_request("GET", config.NOMINATIM_URL, timeout=config.REQUEST_TIMEOUT, params=params)
    _last_nominatim = time.time()
    results = resp.json()
    if not results:
        raise POIError(f"Nominatim returned no results for '{province}, {country}'. Check the spelling.")

    scored = sorted(((_score_candidate(r, province, country), r) for r in results),
                    key=lambda x: x[0], reverse=True)
    if scored[0][0] <= 0:
        found = ", ".join(f"{r.get('display_name')} [{r.get('osm_type')}/{r.get('category') or r.get('class')}={r.get('type')}]"
                          for _, r in scored[:5])
        raise POIError(f"No administrative boundary matching '{province}' in {country}. Candidates: {found}")
    best = scored[0][1]
    boundary = {
        "osm_id": int(best["osm_id"]),
        "osm_type": best["osm_type"],
        "name": (best.get("namedetails") or {}).get("name") or best.get("name") or province,
        "display_name": best.get("display_name"),
        "admin_level": (best.get("extratags") or {}).get("admin_level"),
        "geojson": best.get("geojson"),
    }
    log.info("Boundary: %s (relation %d, admin_level=%s)", boundary["display_name"],
             boundary["osm_id"], boundary["admin_level"])
    cache_file.write_text(json.dumps(boundary), encoding="utf-8")
    return boundary


def get_osm_area(boundary):
    """Overpass area id for an OSM relation (relation id + 3600000000)."""
    return 3600000000 + boundary["osm_id"]


def _simplify(points, max_points):
    """Evenly thin a ring down to max_points (keeps shape adequate for a search filter)."""
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    out = [points[int(i * step)] for i in range(max_points)]
    if out[0] != out[-1]:
        out.append(out[0])
    return out


def boundary_polygons(boundary):
    """Outer rings as Overpass 'poly' strings ('lat lon lat lon ...')."""
    geom = boundary.get("geojson") or {}
    if geom.get("type") == "Polygon":
        polys = [geom["coordinates"]]
    elif geom.get("type") == "MultiPolygon":
        polys = geom["coordinates"]
    else:
        raise POIError("Boundary has no polygon geometry; cannot use polygon fallback.")
    out = []
    for p in polys:
        ring = _simplify(p[0], config.POLY_MAX_POINTS)
        if len(ring) >= 4:
            out.append(" ".join(f"{lat:.5f} {lon:.5f}" for lon, lat in ring))
    return out


# --------------------------------------------------------------------------- query
def parse_tag(tag):
    if "=" not in tag:
        raise POIError(f"Invalid tag '{tag}'. Use the format key=value, e.g. amenity=school.")
    k, v = tag.split("=", 1)
    k, v = k.strip(), v.strip()
    if not k or not v:
        raise POIError(f"Invalid tag '{tag}'. Both key and value are required.")
    return k, v


def _esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def build_overpass_query(tags, area_id=None, polygons=None, timeout=None):
    """Build Overpass QL for node/way/relation (nwr) with the given [(key, value)] tags.

    Uses the area filter when area_id is given, otherwise (fallback) a polygon filter.
    """
    timeout = timeout or config.OVERPASS_QUERY_TIMEOUT
    lines = [f"[out:json][timeout:{timeout}];"]
    if area_id is not None:
        lines.append(f"area({area_id})->.searchArea;")
    lines.append("(")
    for k, v in tags:
        flt = f'["{_esc(k)}"]' if v == "*" else f'["{_esc(k)}"="{_esc(v)}"]'
        if area_id is not None:
            lines.append(f"  nwr{flt}(area.searchArea);")
        else:
            for poly in polygons:
                lines.append(f'  nwr{flt}(poly:"{poly}");')
    lines += [");", "out center tags;"]
    return "\n".join(lines)


def download_poi(query, on_wait=None):
    """Send query to the configured Overpass endpoints; returns parsed JSON.

    on_wait(seconds) is optionally called every second while waiting for the server.
    """
    errors = []
    for endpoint in config.OVERPASS_ENDPOINTS:
        try:
            log.debug("Overpass endpoint: %s", endpoint)
            with Spinner("Waiting for Overpass", on_wait):
                resp = http_request("POST", endpoint, timeout=config.OVERPASS_HTTP_TIMEOUT,
                                    data={"data": query})
            try:
                data = resp.json()
            except ValueError:
                raise POIError(f"Invalid (non-JSON) response from {endpoint}: {resp.text[:200]}")
            remark = data.get("remark", "")
            if remark and ("timed out" in remark or "runtime error" in remark or "out of memory" in remark):
                raise POIError(f"Overpass reported: {remark}")
            return data
        except POIError as exc:
            log.warning("Endpoint %s failed: %s", endpoint, exc)
            errors.append(str(exc))
            time.sleep(config.REQUEST_DELAY)
    raise POIError("All Overpass endpoints failed:\n  - " + "\n  - ".join(errors))


# --------------------------------------------------------------------------- parsing
def parse_osm_response(data):
    """Return list of raw OSM elements."""
    return data.get("elements", []) if isinstance(data, dict) else []


def normalize_poi_data(elements, category, province, country):
    """Convert OSM elements to flat POI dicts. Elements without coordinates are skipped."""
    today = date.today().isoformat()
    rows, skipped = [], 0
    for el in elements:
        etype = el.get("type")
        if etype == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if etype not in ("node", "way", "relation") or lat is None or lon is None:
            skipped += 1
            continue
        tags = el.get("tags") or {}
        rows.append({
            "osm_id": el["id"], "osm_type": etype,
            "name": tags.get("name", ""), "category": category,
            "osm_tags": json.dumps(tags, ensure_ascii=False, sort_keys=True),
            "province": province, "country": country,
            "latitude": float(lat), "longitude": float(lon),
            "source": "OpenStreetMap",
            "osm_url": f"https://www.openstreetmap.org/{etype}/{el['id']}",
            "download_date": today,
        })
    if skipped:
        log.warning("Skipped %d element(s) without coordinates", skipped)
    return rows


def remove_duplicates(rows):
    seen, out = set(), []
    for r in rows:
        k = (r["osm_type"], r["osm_id"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


# --------------------------------------------------------------------------- export
def export_to_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)


def export_to_geojson(rows, path):
    features = []
    for r in rows:
        props = {k: v for k, v in r.items() if k not in ("latitude", "longitude", "osm_tags")}
        props["osm_tags"] = json.loads(r["osm_tags"])
        props["latitude"], props["longitude"] = r["latitude"], r["longitude"]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["longitude"], r["latitude"]]},
            "properties": props,
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------- orchestration
def resolve_jobs(categories, custom_tags):
    """Return list of (label, [(key, value), ...]) – one dataset per category / custom tag."""
    jobs = []
    for cat in categories or []:
        key = cat.strip().lower().replace(" ", "_")
        if key not in config.CATEGORY_MAPPING:
            raise POIError(
                f"Unknown category '{cat}'. Available: {', '.join(sorted(config.CATEGORY_MAPPING))}.\n"
                f"You can use any OSM tag via --tag key=value (e.g. --tag amenity=cafe), "
                f"or add the category to CATEGORY_MAPPING in config.py.")
        jobs.append((key, [parse_tag(t) for t in config.CATEGORY_MAPPING[key]]))
    for t in custom_tags or []:
        k, v = parse_tag(t)
        jobs.append((f"{slugify(k)}_{slugify(v) or 'any'}", [(k, v)]))
    return jobs


def fetch_elements(tags, boundary, on_wait=None):
    """Query Overpass (area filter, polygon fallback) and return raw OSM elements."""
    area_id = get_osm_area(boundary)
    query = build_overpass_query(tags, area_id=area_id)
    log.debug("Query:\n%s", query)
    try:
        data = download_poi(query, on_wait)
        elements = parse_osm_response(data)
        if not elements:
            # the Overpass area index may be missing/stale for this relation -> polygon fallback
            raise POIError("empty result via area query")
    except POIError as exc:
        log.warning("Area query unsuccessful (%s). Trying polygon fallback ...", exc)
        time.sleep(config.REQUEST_DELAY)
        polys = boundary_polygons(boundary)
        data = download_poi(build_overpass_query(tags, polygons=polys), on_wait)
        elements = parse_osm_response(data)
    return elements


def save_rows(rows, base, fmt, out_dir):
    """Write rows to <out_dir>/<base>.csv/.geojson; returns {'csv': Path, 'geojson': Path}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    if fmt in ("csv", "both"):
        files["csv"] = out_dir / f"{base}.csv"
        export_to_csv(rows, files["csv"])
        log.info("  Saved %s", files["csv"])
    if fmt in ("geojson", "both"):
        files["geojson"] = out_dir / f"{base}.geojson"
        export_to_geojson(rows, files["geojson"])
        log.info("  Saved %s", files["geojson"])
    return files


def run_job(label, tags, boundary, province, country, fmt, out_dir):
    elements = fetch_elements(tags, boundary)
    rows = remove_duplicates(normalize_poi_data(elements, label, province, country))
    log.info("  %d raw elements -> %d unique POI", len(elements), len(rows))
    if not rows:
        log.warning("  No POI found for '%s' in %s. Nothing written.", label, province)
        return []
    save_rows(rows, f"{slugify(province)}_{label}", fmt, out_dir)
    return rows


def merge_rows(rows_by_label):
    """Merge rows of several datasets; the same OSM object keeps one row with merged categories."""
    merged = {}
    for rows in rows_by_label.values():
        for r in rows:
            k = (r["osm_type"], r["osm_id"])
            if k in merged:
                cats = merged[k]["category"].split(", ")
                if r["category"] not in cats:
                    merged[k]["category"] += ", " + r["category"]
            else:
                merged[k] = dict(r)
    return list(merged.values())


def run_download(province, country=None, categories=None, tags=None, fmt="both",
                 out_dir=None, progress=None):
    """Programmatic entry point (used by the web app; same pipeline as the CLI).

    progress(stage, percent, message) is called as work advances
    (stages: 1 validate, 2 boundary, 3 download, 4 process, 5 save).
    Returns dict with 'geojson' (FeatureCollection), 'rows', 'files' and 'datasets'.
    Raises POIError on user-facing problems.
    """
    def report(stage, percent, message):
        log.debug("progress %s/%s: %s", stage, percent, message)
        if progress:
            progress(stage, percent, message)

    country = (country or config.DEFAULT_COUNTRY).strip()
    out_dir = Path(out_dir or config.DEFAULT_OUTPUT_DIR)
    province = (province or "").strip()

    report(1, 5, "Memvalidasi input...")
    if not province:
        raise POIError("Nama provinsi wajib diisi.")
    if fmt not in ("csv", "geojson", "both"):
        raise POIError(f"Format '{fmt}' tidak valid. Pilih csv, geojson, atau both.")
    jobs = resolve_jobs(categories, tags)
    if not jobs:
        raise POIError("Masukkan minimal satu kategori atau custom tag (key=value).")
    report(1, 8, f"Input valid: {len(jobs)} dataset ({', '.join(j[0] for j in jobs)}).")

    report(2, 12, f"Mencari batas administrasi {province}, {country} (Nominatim)...")
    boundary = get_province_boundary(province, country)
    province_name = boundary["name"]
    report(2, 25, f"Batas ditemukan: {boundary['display_name']}")

    # stage 3: download every dataset
    raw = {}
    n = len(jobs)
    t0 = time.time()
    for i, (label, jtags) in enumerate(jobs):
        if config.TIME_BUDGET and time.time() - t0 > config.TIME_BUDGET:
            raise POIError(
                f"Batas waktu server ({config.TIME_BUDGET} detik) hampir habis sebelum '{label}' "
                "diproses. Kurangi jumlah kategori per pencarian atau jalankan secara lokal.")
        start = 25 + 55 * i / n
        span = 55 / n
        report(3, round(start), f"Mengambil POI '{label}' ({i + 1}/{n}) dari Overpass...")

        def on_wait(secs, _s=start, _sp=span, _l=label, _i=i):
            report(3, round(_s + min(_sp * 0.9, secs / 5)),
                   f"Menunggu Overpass untuk '{_l}' ({_i + 1}/{n})... {secs} detik")
        raw[label] = fetch_elements(jtags, boundary, on_wait)
        report(3, round(start + span), f"'{label}': {len(raw[label])} objek diterima.")
        if i < n - 1:
            time.sleep(config.REQUEST_DELAY)

    # stage 4: normalise / de-duplicate
    report(4, 84, "Memproses koordinat dan menghapus duplikasi...")
    rows_by_label = {}
    datasets = []
    for label, elements in raw.items():
        rows = remove_duplicates(normalize_poi_data(elements, label, province_name, country))
        log.info("  %s: %d raw elements -> %d unique POI", label, len(elements), len(rows))
        rows_by_label[label] = rows
        datasets.append({"label": label, "count": len(rows)})
    all_rows = merge_rows(rows_by_label)
    if not all_rows:
        raise POIError(f"Tidak ada POI ditemukan untuk {', '.join(raw)} di {province_name}. "
                       "Periksa tag atau coba kategori lain. Tidak ada file yang ditulis.")

    # stage 5: save (per dataset, plus a combined file when several datasets)
    report(5, 90, "Menyimpan CSV dan GeoJSON ke folder output...")
    slug = slugify(province_name)
    files = {}
    for label, rows in rows_by_label.items():
        if rows:
            f = save_rows(rows, f"{slug}_{label}", fmt, out_dir)
            for d in datasets:
                if d["label"] == label:
                    d["files"] = {k: v.name for k, v in f.items()}
            files = {k: v.name for k, v in f.items()}
    if len([r for r in rows_by_label.values() if r]) > 1:
        base = f"{slug}_" + "_".join(l for l, r in rows_by_label.items() if r)
        files = {k: v.name for k, v in save_rows(all_rows, base[:120], fmt, out_dir).items()}
    report(5, 96, "Data tersimpan.")

    # GeoJSON returned to the frontend = exactly what export_to_geojson writes
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "r.geojson"
        export_to_geojson(all_rows, p)
        geojson = json.loads(p.read_text(encoding="utf-8"))
    # CSV text is returned too, so the client never depends on files left on the server
    # (on serverless hosting the saved files live in a non-persistent temp dir).
    csv_text = None
    if "csv" in files:
        csv_text = (out_dir / files["csv"]).read_text(encoding="utf-8-sig")
    return {
        "province": province_name, "country": country,
        "boundary": boundary["display_name"], "count": len(all_rows),
        "datasets": datasets, "files": files, "geojson": geojson, "csv": csv_text,
        "rows": all_rows,
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Download OSM POI inside an Indonesian province boundary.")
    p.add_argument("--province", required=True, help='Province name, e.g. "Jawa Barat"')
    p.add_argument("--country", default=config.DEFAULT_COUNTRY, help="Country (default: Indonesia)")
    p.add_argument("--category", nargs="+", help="One or more categories: "
                   + ", ".join(sorted(config.CATEGORY_MAPPING)))
    p.add_argument("--tag", nargs="+", help="Custom OSM tag(s) key=value, e.g. amenity=school shop=supermarket")
    p.add_argument("--output-dir", default=str(config.DEFAULT_OUTPUT_DIR), help="Output folder (default: ./output)")
    p.add_argument("--format", choices=["csv", "geojson", "both"], default="both", help="Output format")
    p.add_argument("--overpass-url", help="Override Overpass endpoint")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if not args.category and not args.tag:
        p.error("provide at least one --category or --tag")
    return args


def main(argv=None):
    args = parse_args(argv)
    setup_logging(args.verbose)
    if args.overpass_url:
        config.OVERPASS_ENDPOINTS = [args.overpass_url]
    try:
        jobs = resolve_jobs(args.category, args.tag)
        boundary = get_province_boundary(args.province, args.country)
        province = boundary["name"]
        total = 0
        for i, (label, tags) in enumerate(jobs, 1):
            log.info("[%d/%d] Downloading '%s' (%s) in %s", i, len(jobs), label,
                     ", ".join(f"{k}={v}" for k, v in tags), province)
            total += len(run_job(label, tags, boundary, province, args.country,
                                 args.format, Path(args.output_dir)))
            if i < len(jobs):
                time.sleep(config.REQUEST_DELAY)
        log.info("Done. %d POI saved in total.", total)
        return 0
    except POIError as exc:
        log.error("ERROR: %s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
