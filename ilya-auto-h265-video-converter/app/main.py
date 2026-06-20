from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import SettingsStore
from .db import Database
from .worker import Worker


DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("auto_h265")

settings_store = SettingsStore(DATA_DIR)
db = Database(DATA_DIR / "app.db")
worker = Worker(settings_store, db, logger)

app = FastAPI(title="Auto H265 Video Converter", version=__version__)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.on_event("startup")
def on_startup() -> None:
    db.init()
    worker.start_thread()


@app.on_event("shutdown")
def on_shutdown() -> None:
    worker.stop_thread()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__, "worker": worker.status()}


@app.get("/api/version")
def version() -> dict[str, str]:
    return {"version": __version__}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return settings_store.as_dict()


@app.put("/api/settings")
def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    return settings_store.update(patch).__dict__


@app.post("/api/worker/start")
def worker_start() -> dict[str, Any]:
    settings = worker.enable_worker()
    return {"ok": True, "settings": settings.__dict__, "worker": worker.status()}


@app.post("/api/worker/stop")
def worker_stop() -> dict[str, Any]:
    settings = worker.disable_worker()
    return {"ok": True, "settings": settings.__dict__, "worker": worker.status()}


@app.post("/api/worker/pause")
def worker_pause() -> dict[str, Any]:
    settings = worker.pause_auto_convert()
    return {"ok": True, "settings": settings.__dict__, "worker": worker.status()}


@app.post("/api/worker/resume")
def worker_resume() -> dict[str, Any]:
    settings = worker.resume_auto_convert()
    return {"ok": True, "settings": settings.__dict__, "worker": worker.status()}


@app.post("/api/worker/restart")
def worker_restart() -> dict[str, Any]:
    worker.disable_worker()
    settings = worker.enable_worker()
    return {"ok": True, "settings": settings.__dict__, "worker": worker.status()}


@app.post("/api/scan")
def scan_now() -> dict[str, Any]:
    return worker.scan_once()


@app.get("/api/scan/status")
def scan_status() -> dict[str, Any]:
    status = worker.status()
    return {
        "last_scan_at": status.get("last_scan_at"),
        "worker_enabled": status.get("worker_enabled"),
        "auto_convert_enabled": status.get("auto_convert_enabled"),
    }


@app.get("/api/jobs")
def list_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return db.list_jobs(status=status, limit=limit)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int) -> dict[str, Any]:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int) -> dict[str, Any]:
    try:
        return worker.retry_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found")


@app.post("/api/jobs/{job_id}/skip")
def skip_job(job_id: int) -> dict[str, Any]:
    try:
        return worker.skip_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found")


@app.post("/api/jobs/{job_id}/move-to-failed")
def move_to_failed(job_id: int) -> dict[str, Any]:
    try:
        return worker.move_job_to_failed(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Job not found")


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    return db.stats()


@app.get("/api/progress")
def progress() -> dict[str, Any]:
    current = None
    current_id = worker.status().get("current_job_id")
    if current_id:
        current = db.get_job(int(current_id))
    return {
        "worker": worker.status(),
        "current": current,
        "stats": db.stats(),
        "settings": settings_store.as_dict(),
    }


@app.get("/api/logs")
def logs(lines: int = Query(default=200, ge=1, le=2000)) -> PlainTextResponse:
    path = LOG_DIR / "app.log"
    if not path.exists():
        return PlainTextResponse("")
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        tail = fh.readlines()[-lines:]
    return PlainTextResponse("".join(tail))
