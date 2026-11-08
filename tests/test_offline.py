"""Offline tests (mocked network). Run: python -m unittest tests.test_offline -v

The mocks only replace the network layer; all other code (query building, parsing, dedupe,
export, API, SSE) is the real implementation. Mock data is NOT used for the live run.
"""
import csv
import json
import tempfile
import threading
import time
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
        with app_module._jobs_lock:
            app_module._jobs.clear()
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


class CancelTests(Base):
    """Cancellation: backend job must really stop, leave no files, and release the lock."""

    def _slow_download(self, query, on_wait=None):
        self.slow_calls += 1
        for _ in range(400):          # up to ~20 s unless cancelled
            dl._sleep(0.05)           # raises Cancelled as soon as the job is cancelled
        return {"elements": []}

    def setUp(self):
        super().setUp()
        self.slow_calls = 0

    def _stream_and_cancel(self, categories, delay=0.4):
        """Start a streaming search; cancel it from another thread. Returns (events, cancel_response)."""
        holder = {}

        def canceller():
            deadline = time.time() + 10
            while time.time() < deadline:
                with app_module._jobs_lock:
                    running = [j for j in app_module._jobs.values() if not j.finished]
                if running and self.slow_calls:
                    time.sleep(delay)
                    holder["resp"] = self.client.post(f"/api/poi/cancel/{running[0].id}")
                    holder["job"] = running[0]
                    return
                time.sleep(0.02)

        t = threading.Thread(target=canceller)
        t.start()
        with self.client.stream("POST", "/api/poi", json={"province": "Testprov", "category": categories,
                                                          "stream": True}) as r:
            events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]
        t.join()
        return events, holder

    def test_cancel_stops_backend_and_leaves_no_files(self):
        dl.download_poi.side_effect = self._slow_download
        started = time.time()
        events, h = self._stream_and_cancel(["airport", "hospital"])
        self.assertLess(time.time() - started, 5)                 # did not run to completion
        self.assertEqual(events[0]["type"], "job")
        self.assertEqual(events[-1]["type"], "cancelled")
        self.assertEqual(events[-1]["status"], "cancelled")
        self.assertEqual(h["resp"].status_code, 200)
        self.assertEqual(h["resp"].json()["status"], "cancelling")
        self.assertEqual(h["job"].status, "cancelled")
        self.assertEqual(self.slow_calls, 1)                       # next category never started
        self.assertNotIn("result", [e["type"] for e in events])
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])  # no partial/staging files
        self.assertFalse(app_module._busy.locked())                # lock released

    def test_new_search_after_cancel(self):
        dl.download_poi.side_effect = self._slow_download
        self._stream_and_cancel(["airport"])
        dl.download_poi.side_effect = fake_download
        r = self.client.post("/api/poi", json={"province": "Testprov", "category": ["airport"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["count"], 3)

    def test_cancel_endpoint_validation(self):
        self.assertEqual(self.client.post("/api/poi/cancel/not-a-job").status_code, 400)
        self.assertEqual(self.client.post("/api/poi/cancel/" + "0" * 32).status_code, 404)
        # finished job is not cancellable and is left untouched
        with self.client.stream("POST", "/api/poi", json={"province": "Testprov", "category": ["airport"],
                                                          "stream": True}) as r:
            events = [json.loads(l[5:]) for l in r.iter_lines() if l.startswith("data:")]
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual({e["status"] for e in events if e["type"] == "progress"}, {"processing"})
        self.assertEqual(events[-1]["status"], "completed")
        jid = events[0]["job_id"]
        resp = self.client.post(f"/api/poi/cancel/{jid}")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.client.get(f"/api/poi/status/{jid}").json()["status"], "completed")

    def test_cancel_only_targets_its_own_job(self):
        a, b = app_module._register_job(), app_module._register_job()
        self.assertEqual(self.client.post(f"/api/poi/cancel/{a.id}").status_code, 200)
        self.assertTrue(a.cancel_event.is_set())
        self.assertFalse(b.cancel_event.is_set())

    def test_cancel_before_saving_keeps_previous_results(self):
        out = Path(self.tmp.name)
        (out / "testprov_airport.csv").write_text("OLD", encoding="utf-8")   # earlier download
        ev = threading.Event()
        real_save = dl.save_rows

        def save_then_cancel(*a, **k):
            res = real_save(*a, **k)
            ev.set()                                   # cancellation arrives mid-save
            return res

        with mock.patch.object(dl, "save_rows", side_effect=save_then_cancel):
            with self.assertRaises(dl.Cancelled):
                dl.run_download("Testprov", None, ["airport", "hospital"], [], "both", out, cancel_event=ev)
        self.assertEqual([p.name for p in out.iterdir()], ["testprov_airport.csv"])
        self.assertEqual((out / "testprov_airport.csv").read_text(encoding="utf-8"), "OLD")

    def test_cancel_before_each_category(self):
        ev = threading.Event()
        calls = []

        def dl_then_cancel(query, on_wait=None):
            calls.append(query)
            ev.set()
            return {"elements": [{"type": "node", "id": 1, "lat": 1, "lon": 1, "tags": {}}]}

        dl.download_poi.side_effect = dl_then_cancel
        with self.assertRaises(dl.Cancelled):
            dl.run_download("Testprov", None, ["airport", "hospital"], [], "both",
                            Path(self.tmp.name), cancel_event=ev)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])


class HttpCancelTests(unittest.TestCase):
    """http_request: no retry after cancel; a blocking request is abandoned promptly."""

    def test_no_retry_after_cancel(self):
        ev = threading.Event()
        calls = []

        def failing(*a, **k):
            calls.append(1)
            ev.set()
            raise dl.requests.ConnectionError("boom")

        token = dl._cancel_event.set(ev)
        try:
            with mock.patch.object(dl.requests, "request", side_effect=failing), \
                 mock.patch.object(dl.config, "RETRY_BACKOFF", 5):
                started = time.time()
                with self.assertRaises(dl.Cancelled):
                    dl.http_request("GET", "http://x", timeout=5)
        finally:
            dl._cancel_event.reset(token)
        self.assertEqual(len(calls), 1)
        self.assertLess(time.time() - started, 2)   # did not wait for the 5 s backoff

    def test_blocking_request_is_abandoned(self):
        ev = threading.Event()
        token = dl._cancel_event.set(ev)
        try:
            with mock.patch.object(dl.requests, "request", side_effect=lambda *a, **k: time.sleep(3)):
                threading.Timer(0.3, ev.set).start()
                started = time.time()
                with self.assertRaises(dl.Cancelled):
                    dl.http_request("GET", "http://x", timeout=5)
        finally:
            dl._cancel_event.reset(token)
        self.assertLess(time.time() - started, 2)

    def test_without_cancel_event_behaves_as_before(self):
        class R:
            status_code = 200
            def raise_for_status(self): pass
        with mock.patch.object(dl.requests, "request", return_value=R()):
            self.assertIsInstance(dl.http_request("GET", "http://x", timeout=5), R)


class FrontendTests(Base):
    def test_theme_and_cancel_markup(self):
        html = self.client.get("/").text
        for needle in ('id="themeToggle"', "poi-theme", 'data-theme', "--bg-primary", "--card-background",
                       "--button-background", 'id="cancelSearch"', "/api/poi/cancel/",
                       "Cancelling search...", "Search cancelled successfully",
                       "openstreetmap.org/copyright"):
            self.assertIn(needle, html, needle)


if __name__ == "__main__":
    unittest.main()
