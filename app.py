"""FastAPI backend for POI Explorer (local + Vercel).

Local:  python -m uvicorn app:app --host 127.0.0.1 --port 8000
Vercel: detected automatically (FastAPI instance named `app` in app.py).

POST /api/poi calls download_poi.run_download(), the same pipeline used by the CLI.
With "stream": true the response is Server-Sent Events (real progress); otherwise plain JSON.
Every streamed search gets a unique job id (first SSE event: {"type": "job", "job_id": ...}).
POST /api/poi/cancel/{job_id} sets the job's cancellation event; the downloader stops at the next
safe checkpoint and the stream ends with {"type": "cancelled", "status": "cancelled"}.
SSE "status" values: processing | completed | cancelled | error.
The result contains the GeoJSON and the CSV text, so users download data directly from the
response instead of from server files (serverless filesystems are not persistent).
"""
import json
import logging
import queue
import re
import threading
import time
import uuid
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

# ---- job registry (in-memory; per server process) ----
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_FINISHED_JOBS = 50


class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex  # unguessable: only the creator knows it
        self.status = "processing"  # processing | completed | cancelled | error
        self.cancel_event = threading.Event()
        self.created = time.time()

    @property
    def finished(self):
        return self.status != "processing"


_jobs = {}
_jobs_lock = threading.Lock()


def _register_job():
    job = Job()
    with _jobs_lock:
        done = [j for j in _jobs.values() if j.finished]
        for old in sorted(done, key=lambda j: j.created)[:max(0, len(done) - _MAX_FINISHED_JOBS)]:
            _jobs.pop(old.id, None)
        _jobs[job.id] = job
    return job


def _get_job(job_id):
    if not _JOB_ID_RE.match(job_id or ""):
        raise HTTPException(status_code=400, detail="Job ID tidak valid.")
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job tidak ditemukan.")
    return job


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
        self.sink({"type": "progress", "status": "processing", "stage": stage,
                   "percent": percent, "message": message})

    def note(self, message):
        self.sink({"type": "progress", "status": "processing", "stage": self.stage,
                   "percent": self.percent, "message": message})


def _public(result):
    return {k: v for k, v in result.items() if k != "rows"}


def _execute(req: POIRequest, sink, cancel_event=None):
    """Run the downloader; returns public result dict. Raises dl.POIError."""
    reporter = Reporter(sink)
    handler = _WarningForwarder(reporter)
    dl.log.addHandler(handler)
    try:
        result = dl.run_download(
            req.province, req.country, req.category, req.tags, req.format,
            OUTPUT_DIR, progress=reporter, cancel_event=cancel_event)
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

    job = _register_job()
    q: "queue.Queue[Optional[dict]]" = queue.Queue()
    q.put({"type": "job", "status": "processing", "job_id": job.id})

    def worker():
        status = "error"
        final = {"type": "error", "status": "error", "message": "Kesalahan internal."}
        try:
            result = _execute(req, q.put, job.cancel_event)
            status = "completed"
            final = {"type": "result", "status": "completed", "data": result}
        except dl.Cancelled:
            dl.log.info("Job %s cancelled", job.id)
            status = "cancelled"
            final = {"type": "cancelled", "status": "cancelled", "job_id": job.id,
                     "message": "Search cancelled successfully"}
        except dl.POIError as exc:
            dl.log.error("ERROR: %s", exc)
            final = {"type": "error", "status": "error", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            dl.log.exception("Unexpected error")
            final = {"type": "error", "status": "error", "message": f"Kesalahan internal: {exc}"}
        finally:
            # Release the lock BEFORE announcing the final state, so a client that receives
            # "cancelled"/"result"/"error" can start a new search immediately.
            _busy.release()
            job.status = status
            q.put(final)
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def events():
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    return
                yield _sse(item)
        finally:
            # Client disconnected while the job is still running: stop the job as well,
            # otherwise it would keep the processing lock without anybody listening.
            if not job.finished:
                job.cancel_event.set()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/poi/cancel/{job_id}")
def api_poi_cancel(job_id: str):
    """Request cancellation of a running job. The job stops at the next safe checkpoint;
    the final confirmation ("cancelled") is delivered by the job's SSE stream."""
    job = _get_job(job_id)
    if job.status == "cancelled":
        return {"job_id": job.id, "status": "cancelled", "cancel_requested": True}
    if job.finished:
        raise HTTPException(status_code=409, detail={
            "job_id": job.id, "status": job.status,
            "message": "Job sudah selesai; tidak dapat dibatalkan."})
    job.cancel_event.set()
    return {"job_id": job.id, "status": "cancelling", "cancel_requested": True}


@app.get("/api/poi/status/{job_id}")
def api_poi_status(job_id: str):
    job = _get_job(job_id)
    status = "cancelling" if (job.status == "processing" and job.cancel_event.is_set()) else job.status
    return {"job_id": job.id, "status": status}


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
