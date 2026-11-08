"""End-to-end cancellation check over real HTTP + SSE (uvicorn started in-process).

    python tests/e2e_cancel.py --mock   # Overpass replaced by a slow fake (no network needed)
    python tests/e2e_cancel.py          # live: airport in Jawa Barat via the real Nominatim/Overpass

Starts a search, waits until progress reaches ~20-40 %, calls POST /api/poi/cancel/{job_id} and
verifies: final SSE event is "cancelled", the backend lock is released, the output folder holds
no (partial) files, and a new search can start afterwards.
Runtime data goes to a temporary POI_DATA_DIR so the project's own output/ is never touched.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

ap = argparse.ArgumentParser()
ap.add_argument("--mock", action="store_true")
ap.add_argument("--port", type=int, default=8765)
args = ap.parse_args()

tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["POI_DATA_DIR"] = tmp.name
import uvicorn  # noqa: E402
import app as app_module  # noqa: E402
import download_poi as dl  # noqa: E402

if args.mock:
    BOUNDARY = {"osm_id": 1, "osm_type": "relation", "name": "Testprov", "display_name": "Testprov",
                "admin_level": "4", "geojson": None}
    dl.get_province_boundary = lambda *a, **k: BOUNDARY
    mode = {"slow": True}

    def fake_download(query, on_wait=None):
        if mode["slow"]:
            for i in range(600):
                dl._sleep(0.05)
                if on_wait and i % 20 == 0:
                    on_wait(i // 20)
        return {"elements": [{"type": "node", "id": 1, "lat": -6.1, "lon": 107.1, "tags": {"name": "A"}}]}

    dl.download_poi = fake_download
    PROVINCE = "Testprov"
else:
    PROVINCE = "Jawa Barat"

BASE = f"http://127.0.0.1:{args.port}"
server = uvicorn.Server(uvicorn.Config(app_module.app, host="127.0.0.1", port=args.port, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/api/health", timeout=1)
        break
    except Exception:
        time.sleep(0.25)

out_dir = Path(app_module.OUTPUT_DIR)


def files():
    return sorted(str(p.relative_to(out_dir)) for p in out_dir.rglob("*") if p.is_file()) if out_dir.exists() else []


def post(path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(BASE + path, data, {"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def search_and_cancel(categories, cancel_at=(20, 40)):
    req = urllib.request.Request(BASE + "/api/poi", json.dumps(
        {"province": PROVINCE, "category": categories, "stream": True}).encode(),
        {"Content-Type": "application/json"}, method="POST")
    events, cancelled_sent = [], False
    t_cancel = None
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            events.append(ev)
            if ev["type"] == "progress" and not cancelled_sent:
                print(f"  progress {ev['percent']:>3}%  {ev['message'][:70]}")
            if (ev["type"] == "progress" and ev["percent"] >= cancel_at[0] and not cancelled_sent):
                job_id = events[0]["job_id"]
                t_cancel = time.time()
                print("  >> cancel request:", *post(f"/api/poi/cancel/{job_id}"))
                cancelled_sent = True
    return events, t_cancel


ok = True


def check(label, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label)


try:
    print(f"== cancel test ({'mock' if args.mock else 'LIVE'}), province={PROVINCE}")
    events, t_cancel = search_and_cancel(["airport"])
    took = time.time() - t_cancel if t_cancel else None
    check("job id received first", events[0]["type"] == "job" and len(events[0]["job_id"]) == 32)
    check("final SSE event is cancelled", events[-1]["type"] == "cancelled" and events[-1]["status"] == "cancelled")
    check("no result event", all(e["type"] != "result" for e in events))
    print(f"   backend confirmed cancellation {took:.2f}s after the cancel request" if took else "   (no cancel sent)")
    check("no output files (no partial results)", files() == [])
    check("backend lock released", not app_module._busy.locked())
    jid = events[0]["job_id"]
    check("late cancel of the finished job -> 200 cancelled (idempotent)",
          post(f"/api/poi/cancel/{jid}")[0] == 200)

    print("== new search after cancellation")
    if args.mock:
        mode["slow"] = False
    req = urllib.request.Request(BASE + "/api/poi", json.dumps(
        {"province": PROVINCE, "category": ["airport"], "stream": True}).encode(),
        {"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        evs = [json.loads(l.decode().strip()[5:]) for l in r if l.decode().startswith("data:")]
    check("new search completes", evs[-1]["type"] == "result" and evs[-1]["data"]["count"] > 0)
    check("files written only for the completed search", len(files()) == 2)
finally:
    server.should_exit = True
    time.sleep(0.5)
    tmp.cleanup()
print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
