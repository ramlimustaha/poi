"""FastAPI backend for POI Explorer (local + Vercel).

Local:  python -m uvicorn app:app --host 127.0.0.1 --port 8000
Vercel: detected automatically (FastAPI instance named `app` in app.py).

POST /api/poi calls download_poi.run_download(), the same pipeline used by the CLI.
With "stream": true the response is Server-Sent Events (real progress); otherwise plain JSON.
The result contains the GeoJSON and the CSV text, so users download data directly from the
response instead of from server files (serverless filesystems are not persistent).
"""
import json
import logging
import queue
import threading
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import download_poi as dl

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = Path(config.DEFAULT_OUTPUT_DIR)  # /tmp/... on Vercel (non-persistent)

dl.SHOW_SPINNER = False
dl.setup_logging()

app = FastAPI(title="POI Explorer", version="1.0")

# Only one Overpass download at a time (do not hammer public servers).
_busy = threading.Lock()


class POIRequest(BaseModel):
    province: str = ""
    country: str = config.DEFAULT_COUNTRY
    category: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    format: str = "both"
    stream: bool = False


class _WarningForwarder(logging.Handler):
    """Forwards downloader warnings (retries, fallbacks) to the progress stream."""

    def __init__(self, reporter):
        super().__init__(level=logging.WARNING)
        self.reporter = reporter

    def emit(self, record):
        self.reporter.note(f"⚠ {record.getMessage()[:200]}")


class Reporter:
    def __init__(self, sink):
        self.sink = sink
        self.stage, self.percent = 1, 0

    def __call__(self, stage, percent, message):
        self.stage, self.percent = stage, percent
        self.sink({"type": "progress", "stage": stage, "percent": percent, "message": message})

    def note(self, message):
        self.sink({"type": "progress", "stage": self.stage, "percent": self.percent, "message": message})


def _public(result):
    return {k: v for k, v in result.items() if k != "rows"}


def _execute(req: POIRequest, sink):
    """Run the downloader; returns public result dict. Raises dl.POIError."""
    reporter = Reporter(sink)
    handler = _WarningForwarder(reporter)
    dl.log.addHandler(handler)
    try:
        result = dl.run_download(
            req.province, req.country, req.category, req.tags, req.format,
            OUTPUT_DIR, progress=reporter)
    finally:
        dl.log.removeHandler(handler)
    return _public(result)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/poi")
def api_poi(req: POIRequest):
    if not _busy.acquire(blocking=False):
        msg = "Server sedang memproses unduhan lain. Tunggu hingga selesai lalu coba lagi."
        if req.stream:
            return StreamingResponse(iter([_sse({"type": "error", "message": msg})]),
                                     media_type="text/event-stream")
        raise HTTPException(status_code=409, detail=msg)

    if not req.stream:
        try:
            return _execute(req, lambda e: None)
        except dl.POIError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            dl.log.exception("Unexpected error")
            raise HTTPException(status_code=500, detail=f"Kesalahan internal: {exc}")
        finally:
            _busy.release()

    q: "queue.Queue[Optional[dict]]" = queue.Queue()

    def worker():
        try:
            result = _execute(req, q.put)
            q.put({"type": "result", "data": result})
        except dl.POIError as exc:
            dl.log.error("ERROR: %s", exc)
            q.put({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            dl.log.exception("Unexpected error")
            q.put({"type": "error", "message": f"Kesalahan internal: {exc}"})
        finally:
            _busy.release()
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def events():
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if item is None:
                return
            yield _sse(item)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/categories")
def api_categories():
    return config.CATEGORY_MAPPING


@app.get("/api/health")
def api_health():
    return {"status": "ok", "vercel": config.IS_VERCEL, "output_dir": str(OUTPUT_DIR)}


@app.get("/")
def index():
    page = STATIC_DIR / "poi_map.html"
    if not page.is_file():
        raise HTTPException(status_code=500, detail="static/poi_map.html tidak ditemukan pada deployment.")
    return FileResponse(page, media_type="text/html")


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
