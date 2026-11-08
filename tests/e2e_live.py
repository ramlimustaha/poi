"""Live end-to-end check (real network) simulating the Vercel runtime (VERCEL=1).

Starts uvicorn with VERCEL=1 (all writable paths -> system temp dir), then calls
POST /api/poi with SSE. Verifies results, CSV/GeoJSON from the response, and that the
project's own output/ folder is untouched.
"""
import csv, io, json, os, subprocess, sys, time, urllib.request, collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")
env = {**os.environ, "VERCEL": "1"}
srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"],
                       cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
BASE = "http://127.0.0.1:8000"


def call(payload, show=True):
    body = json.dumps({**payload, "stream": True}).encode()
    req = urllib.request.Request(BASE + "/api/poi", body, {"Content-Type": "application/json"})
    result, last, evs = None, None, []
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            evs.append(ev)
            if ev["type"] == "progress":
                key = (ev["stage"], ev["message"][:50])
                if show and key != last and "Menunggu" not in ev["message"]:
                    print(f"  [{ev['stage']}/6 {ev['percent']:>3}%] {ev['message'][:90]}")
                last = key
            elif ev["type"] == "result":
                result = ev["data"]
    return result, evs


try:
    for _ in range(40):
        try:
            html = urllib.request.urlopen(BASE + "/", timeout=2).read().decode()
            break
        except Exception:
            time.sleep(0.5)
    print("health:", json.loads(urllib.request.urlopen(BASE + "/api/health").read()))
    print("index OK:", "POI Explorer" in html, "| no direct overpass in html:", "overpass-api.de" not in html)

    before = sorted(p.name for p in (ROOT / "output").glob("*")) if (ROOT / "output").exists() else []
    result, _ = call({"province": "Jawa Barat", "category": ["airport", "hospital"], "format": "both"})
    feats = result["geojson"]["features"]
    ids = [(f["properties"]["osm_type"], f["properties"]["osm_id"]) for f in feats]
    print("features:", len(feats), "| dup:", len(ids) - len(set(ids)), "| datasets:",
          [(d["label"], d["count"]) for d in result["datasets"]])
    print("types:", dict(collections.Counter(i[0] for i in ids)), "| files:", result["files"])
    rows = list(csv.DictReader(io.StringIO(result["csv"])))
    print("csv rows (from response):", len(rows), "== features:", len(rows) == len(feats))
    print("csv/geojson same ids:", {(r["osm_type"], int(r["osm_id"])) for r in rows} == set((a, b) for a, b in ids))
    after = sorted(p.name for p in (ROOT / "output").glob("*")) if (ROOT / "output").exists() else []
    print("project output/ untouched:", before == after)

    result2, _ = call({"province": "Jawa Barat", "tags": ["tourism=zoo"], "format": "both"}, show=False)
    print("custom tag tourism=zoo:", result2["count"], "POI | datasets:", result2["datasets"])

    _, evs = call({"province": "Jawa Barat", "category": ["kafe"]}, show=False)
    print("error event:", evs[-1]["type"], "|", evs[-1].get("message", "")[:70])
finally:
    srv.terminate()
