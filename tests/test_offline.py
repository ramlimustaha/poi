"""Offline tests (mocked network). Run: python -m unittest tests.test_offline -v

The mocks only replace the network layer; all other code (query building, parsing, dedupe,
export, API, SSE) is the real implementation. Mock data is NOT used for the live run.
"""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app as app_module
import download_poi as dl

BOUNDARY = {"osm_id": 1, "osm_type": "relation", "name": "Testprov",
            "display_name": "Testprov, Indonesia", "admin_level": "4",
            "geojson": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}}

ELEMENTS = {
    "aeroway=aerodrome": [
        {"type": "node", "id": 1, "lat": -6.1, "lon": 107.1, "tags": {"name": "A", "aeroway": "aerodrome"}},
        {"type": "way", "id": 1, "center": {"lat": -6.2, "lon": 107.2}, "tags": {"aeroway": "aerodrome"}},
        {"type": "relation", "id": 1, "center": {"lat": -6.3, "lon": 107.3}, "tags": {"name": "R"}},
        {"type": "node", "id": 1, "lat": -6.1, "lon": 107.1, "tags": {"name": "A"}},  # duplicate
        {"type": "way", "id": 99, "tags": {}},  # no coordinates -> skipped
    ],
    "amenity=hospital": [
        {"type": "node", "id": 1, "lat": -6.1, "lon": 107.1, "tags": {"name": "A", "amenity": "hospital"}},
        {"type": "node", "id": 2, "lat": -6.5, "lon": 107.5, "tags": {"amenity": "hospital"}},
    ],
}


def fake_download(query, on_wait=None):
    assert "area(3600000001)" in query
    for tag, els in ELEMENTS.items():
        k, v = tag.split("=")
        if f'["{k}"="{v}"]' in query:
            if on_wait:
                on_wait(1)
            return {"elements": els}
    return {"elements": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = [mock.patch.object(dl, "get_province_boundary", return_value=BOUNDARY),
             mock.patch.object(dl, "download_poi", side_effect=fake_download),
             mock.patch.object(dl.config, "REQUEST_DELAY", 0),
             mock.patch.object(app_module, "OUTPUT_DIR", Path(self.tmp.name))]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.client = TestClient(app_module.app)


class DownloaderTests(Base):
    def test_query_tags(self):
        q = dl.build_overpass_query([("aeroway", "aerodrome")], area_id=3600000001)
        self.assertIn('nwr["aeroway"="aerodrome"](area.searchArea);', q)
        self.assertIn("out center tags;", q)
        self.assertIn('["amenity"]', dl.build_overpass_query([("amenity", "*")], area_id=1))

    def test_unknown_category_message(self):
        with self.assertRaises(dl.POIError) as c:
            dl.resolve_jobs(["kafe"], None)
        self.assertIn("--tag", str(c.exception))

    def test_run_download_dedupe_and_files(self):
        stages = []
        r = dl.run_download("Testprov", None, ["airport", "hospital"], [], "both",
                            Path(self.tmp.name), progress=lambda s, p, m: stages.append(s))
        self.assertEqual({1, 2, 3, 4, 5}, set(stages))
        ids = [(x["osm_type"], x["osm_id"]) for x in r["rows"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(r["count"], 4)  # node1(merged), way1, rel1, node2
        self.assertEqual(len(r["geojson"]["features"]), 4)
        merged = [f for f in r["geojson"]["features"] if f["properties"]["osm_id"] == 1
                  and f["properties"]["osm_type"] == "node"][0]
        self.assertEqual(merged["properties"]["category"], "airport, hospital")
        out = Path(self.tmp.name)
        for name in ("testprov_airport", "testprov_hospital", "testprov_airport_hospital"):
            self.assertTrue((out / f"{name}.csv").exists(), name)
            self.assertTrue((out / f"{name}.geojson").exists(), name)
        # combined csv == geojson returned
        rows = list(csv.DictReader(open(out / r["files"]["csv"], encoding="utf-8-sig")))
        self.assertEqual(len(rows), 4)
        self.assertEqual(json.load(open(out / r["files"]["geojson"], encoding="utf-8")), r["geojson"])
        # Point geometry is [lon, lat]
        f0 = r["geojson"]["features"][0]["geometry"]["coordinates"]
        self.assertTrue(100 < f0[0] < 110 and f0[1] < 0)


class ApiTests(Base):
    def test_json_mode(self):
        r = self.client.post("/api/poi", json={"province": "Testprov", "category": ["airport"],
                                                "tags": [], "format": "both"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["count"], 3)
        self.assertNotIn("rows", d)
        # CSV text is returned in the response and equals the file written by Python
        self.assertEqual(d["csv"], (Path(self.tmp.name) / d["files"]["csv"]).read_text(encoding="utf-8-sig"))
        self.assertEqual(len(list(csv.DictReader(d["csv"].splitlines()))), 3)
        self.assertEqual(len(d["geojson"]["features"]), 3)
        self.assertEqual(self.client.get("/api/download/x.csv").status_code, 404)  # no file endpoint
        self.assertEqual(self.client.get("/api/health").json()["status"], "ok")

    def test_custom_tag_and_error(self):
        r = self.client.post("/api/poi", json={"province": "Testprov", "tags": ["amenity=hospital"]})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/poi", json={"province": "Testprov", "category": ["kafe"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("--tag", r.json()["detail"])
        r = self.client.post("/api/poi", json={"province": "", "category": ["airport"]})
        self.assertEqual(r.status_code, 400)

    def test_sse(self):
        with self.client.stream("POST", "/api/poi", json={"province": "Testprov",
                                                          "category": ["airport", "hospital"],
                                                          "stream": True}) as r:
            events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]
        types = [e["type"] for e in events]
        self.assertEqual(types[-1], "result")
        self.assertEqual({e["stage"] for e in events if e["type"] == "progress"}, {1, 2, 3, 4, 5})
        pct = [e["percent"] for e in events if e["type"] == "progress"]
        self.assertEqual(pct, sorted(pct))
        self.assertEqual(len(events[-1]["data"]["geojson"]["features"]), 4)

    def test_sse_error(self):
        with self.client.stream("POST", "/api/poi", json={"province": "Testprov",
                                                          "category": ["kafe"], "stream": True}) as r:
            events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("--tag", events[-1]["message"])

    def test_no_path_traversal_or_file_endpoint(self):
        self.assertEqual(self.client.get("/api/download/..%2Fconfig.py").status_code, 404)

    def test_vercel_paths(self):
        """On Vercel all writable paths must be under the system temp dir (read-only /var/task)."""
        import os, subprocess, sys, tempfile
        code = ("import config,tempfile;from pathlib import Path;t=Path(tempfile.gettempdir());"
                "print(all(str(p).startswith(str(t)) for p in (config.DEFAULT_OUTPUT_DIR,config.LOG_DIR,config.CACHE_DIR)),"
                "config.TIME_BUDGET,config.OVERPASS_HTTP_TIMEOUT)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env={**os.environ, "VERCEL": "1"}, cwd=Path(app_module.__file__).parent).stdout.split()
        self.assertEqual(out, ["True", "270", "125"])

    def test_index(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/api/poi", r.text)
        self.assertNotIn("overpass-api.de", r.text)


if __name__ == "__main__":
    unittest.main()
