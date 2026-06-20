from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import Settings, SettingsStore
from .db import Database, utc_now
from .ffmpeg_runner import (
    build_ffmpeg_command,
    output_path_for,
    parse_fps,
    parse_progress_line,
    parse_speed,
    probe_video,
    progress_from_out_time,
    replace_or_move,
    temp_path_for,
    unique_path,
)


class Worker:
    def __init__(self, settings_store: SettingsStore, db: Database, logger: logging.Logger) -> None:
        self.settings_store = settings_store
        self.db = db
        self.logger = logger
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._current_job_id: int | None = None
        self._last_scan_at: str | None = None
        self._last_error: str | None = None

    def start_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="auto-h265-worker", daemon=True)
            self._thread.start()
            self.logger.info("Worker thread started")

    def stop_thread(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def status(self) -> dict[str, Any]:
        settings = self.settings_store.get()
        thread_alive = bool(self._thread and self._thread.is_alive())
        return {
            "thread_alive": thread_alive,
            "worker_enabled": settings.worker_enabled,
            "auto_convert_enabled": settings.auto_convert_enabled,
            "current_job_id": self._current_job_id,
            "last_scan_at": self._last_scan_at,
            "last_error": self._last_error,
        }

    def enable_worker(self) -> Settings:
        settings = self.settings_store.update({"worker_enabled": True})
        self.start_thread()
        return settings

    def disable_worker(self) -> Settings:
        return self.settings_store.update({"worker_enabled": False})

    def pause_auto_convert(self) -> Settings:
        return self.settings_store.update({"auto_convert_enabled": False})

    def resume_auto_convert(self) -> Settings:
        self.start_thread()
        return self.settings_store.update({"auto_convert_enabled": True, "worker_enabled": True})

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                settings = self.settings_store.get()
                if settings.worker_enabled and settings.auto_convert_enabled:
                    self.promote_waiting(settings)
                    self.scan_once(settings)
                    self.process_next(settings)
                time.sleep(settings.scan_interval_seconds)
            except Exception as exc:
                self._last_error = str(exc)
                self.logger.exception("Worker loop failed: %s", exc)
                time.sleep(5)

    def scan_once(self, settings: Settings | None = None) -> dict[str, Any]:
        settings = settings or self.settings_store.get()
        input_dir = Path(settings.input_path)
        input_dir.mkdir(parents=True, exist_ok=True)
        found = 0
        inserted = 0
        pattern_iter = input_dir.rglob("*") if settings.recursive_scan else input_dir.glob("*")
        now = time.time()
        for path in pattern_iter:
            try:
                if not path.is_file():
                    continue
                if path.suffix.lower() not in settings.allowed_extensions:
                    continue
                if ".tmp" in path.name:
                    continue
                stat = path.stat()
                age = now - stat.st_mtime
                status = "pending" if age >= settings.stable_file_seconds else "waiting_file_copy"
                if self.db.add_or_touch_job(str(path.resolve()), stat.st_size, status):
                    inserted += 1
                found += 1
            except OSError as exc:
                self.logger.warning("Cannot scan %s: %s", path, exc)
        self._last_scan_at = utc_now()
        if inserted:
            self.logger.info("Scan found %s new file(s)", inserted)
        return {"found": found, "inserted": inserted, "scanned_at": self._last_scan_at}

    def promote_waiting(self, settings: Settings | None = None) -> int:
        settings = settings or self.settings_store.get()
        promoted = 0
        now = time.time()
        for job in self.db.waiting_jobs():
            path = Path(job["source_path"])
            try:
                if path.exists() and now - path.stat().st_mtime >= settings.stable_file_seconds:
                    self.db.promote_waiting(str(path.resolve()))
                    promoted += 1
            except OSError:
                continue
        return promoted

    def process_next(self, settings: Settings | None = None) -> bool:
        settings = settings or self.settings_store.get()
        job = self.db.get_next_job()
        if not job:
            return False
        self.process_job(job, settings)
        return True

    def process_job(self, job: dict[str, Any], settings: Settings) -> None:
        job_id = int(job["id"])
        source = Path(job["source_path"])
        started = time.time()
        self._current_job_id = job_id
        self.db.update_job(job_id=job_id, status="probing", started_at=utc_now(), error_message=None)
        self.logger.info("Processing job %s: %s", job_id, source)
        try:
            if not source.exists():
                raise RuntimeError("Source file disappeared")
            probe = probe_video(source)
            self.db.update_job(
                job_id=job_id,
                source_codec=probe.codec,
                source_width=probe.width,
                source_height=probe.height,
                source_duration=probe.duration,
                source_size_bytes=probe.size_bytes,
            )
            if probe.codec in {"hevc", "h265"} and probe.width <= settings.max_width and probe.height <= settings.max_height:
                self._handle_already_hevc(job_id, source, probe.size_bytes, settings)
                return
            self._convert(job_id, source, probe, settings, started)
        except Exception as exc:
            self._handle_failure(job, source, settings, exc, started)
        finally:
            self._current_job_id = None

    def _handle_already_hevc(self, job_id: int, source: Path, source_size: int, settings: Settings) -> None:
        if settings.already_hevc_action == "copy":
            output = unique_path(Path(settings.output_path) / source.name)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)
            status = "success"
            output_size = output.stat().st_size
            output_path = str(output)
        else:
            status = "skipped"
            output_size = 0
            output_path = None
        self.db.update_job(
            job_id=job_id,
            status=status,
            output_path=output_path,
            output_size_bytes=output_size,
            progress_percent=100,
            finished_at=utc_now(),
            error_message=None,
            elapsed_seconds=0,
        )
        self.logger.info("Job %s %s because source is already HEVC", job_id, status)

    def _convert(self, job_id: int, source: Path, probe: Any, settings: Settings, started: float) -> None:
        output = output_path_for(source, settings)
        temp_output = temp_path_for(output, settings)
        self.db.update_job(
            job_id=job_id,
            status="converting",
            output_path=str(output),
            temp_output_path=str(temp_output),
            progress_percent=0,
        )
        cmd = build_ffmpeg_command(source, temp_output, probe, settings)
        self.logger.info("Starting FFmpeg for job %s", job_id)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        log_tail: list[str] = []
        assert process.stdout is not None
        for raw_line in process.stdout:
            parsed = parse_progress_line(raw_line)
            if parsed is None:
                clean = raw_line.strip()
                if clean:
                    log_tail = (log_tail + [clean])[-20:]
                continue
            key, value = parsed
            patch: dict[str, Any] = {"elapsed_seconds": round(time.time() - started, 2)}
            if key == "out_time_ms":
                percent = progress_from_out_time(value, float(probe.duration or 0))
                patch["progress_percent"] = round(percent, 2)
                if percent > 0:
                    elapsed = time.time() - started
                    remaining = elapsed * (100 - percent) / percent
                    patch["eta_seconds"] = round(max(0, remaining), 2)
            elif key == "fps":
                patch["fps"] = parse_fps(value)
            elif key == "speed":
                patch["speed"] = parse_speed(value)
            elif key == "progress" and value == "end":
                patch["progress_percent"] = 100
                patch["eta_seconds"] = 0
            self.db.update_job(job_id=job_id, **patch)
        return_code = process.wait()
        if return_code != 0:
            detail = "; ".join(log_tail[-5:]) or f"ffmpeg exited with {return_code}"
            raise RuntimeError(detail)
        if not temp_output.exists():
            raise RuntimeError("FFmpeg finished but output file is missing")
        replace_or_move(temp_output, output)
        output_size = output.stat().st_size
        self.db.update_job(
            job_id=job_id,
            status="success",
            output_path=str(output),
            temp_output_path=None,
            output_size_bytes=output_size,
            progress_percent=100,
            eta_seconds=0,
            finished_at=utc_now(),
            elapsed_seconds=round(time.time() - started, 2),
            error_message=None,
        )
        self.logger.info("Job %s completed: %s", job_id, output)

    def _handle_failure(
        self,
        job: dict[str, Any],
        source: Path,
        settings: Settings,
        exc: Exception,
        started: float,
    ) -> None:
        job_id = int(job["id"])
        latest_job = self.db.get_job(job_id) or job
        retry_count = int(latest_job.get("retry_count") or 0) + 1
        message = str(exc)
        self._last_error = message
        temp_output = latest_job.get("temp_output_path")
        if temp_output:
            try:
                Path(temp_output).unlink(missing_ok=True)
            except OSError:
                pass
        if retry_count >= settings.max_retries:
            failed_path = self._move_to_failed(source, settings)
            self.db.update_job(
                job_id=job_id,
                source_path=str(failed_path) if failed_path else str(source),
                status="moved_to_failed" if failed_path else "failed",
                retry_count=retry_count,
                progress_percent=0,
                finished_at=utc_now(),
                elapsed_seconds=round(time.time() - started, 2),
                error_message=message,
            )
            self.logger.error("Job %s failed and moved to quarantine: %s", job_id, message)
        else:
            self.db.update_job(
                job_id=job_id,
                status="pending",
                retry_count=retry_count,
                progress_percent=0,
                error_message=message,
            )
            self.logger.warning("Job %s failed, retry %s/%s: %s", job_id, retry_count, settings.max_retries, message)

    def _move_to_failed(self, source: Path, settings: Settings) -> Path | None:
        if not source.exists():
            return None
        failed_dir = Path(settings.failed_path)
        failed_dir.mkdir(parents=True, exist_ok=True)
        destination = unique_path(failed_dir / source.name)
        shutil.move(str(source), str(destination))
        return destination

    def retry_job(self, job_id: int) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        self.db.update_job(
            job_id=job_id,
            status="pending",
            retry_count=0,
            progress_percent=0,
            error_message=None,
            finished_at=None,
        )
        return self.db.get_job(job_id) or {}

    def skip_job(self, job_id: int) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        self.db.update_job(job_id=job_id, status="skipped", progress_percent=100, finished_at=utc_now())
        return self.db.get_job(job_id) or {}

    def move_job_to_failed(self, job_id: int) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if not job:
            raise KeyError(job_id)
        settings = self.settings_store.get()
        failed_path = self._move_to_failed(Path(job["source_path"]), settings)
        self.db.update_job(
            job_id=job_id,
            source_path=str(failed_path) if failed_path else job["source_path"],
            status="moved_to_failed",
            finished_at=utc_now(),
        )
        return self.db.get_job(job_id) or {}
