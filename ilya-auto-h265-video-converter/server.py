#!/usr/bin/env python3
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

APP_VERSION = "0.3.1"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.sqlite")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")
FFMPEG_LOG = os.path.join(LOG_DIR, "ffmpeg.log")
APP_LOG = os.path.join(LOG_DIR, "app.log")
HOST_EXTERNAL_ROOT = os.environ.get("HOST_EXTERNAL_ROOT", "/home/umbrel/umbrel/external").rstrip("/")
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "/media").rstrip("/")
HOST_ROOT = os.environ.get("HOST_ROOT", "/host").rstrip("/")

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".m4v", ".webm", ".flv", ".wmv", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".ogv"}
SUCCESS_STATUSES = {"success", "skip_ok", "moved_already_hevc", "copied_already_hevc", "hardlinked_already_hevc", "manual_ok"}
TERMINAL_STATUSES = SUCCESS_STATUSES | {"moved_to_failed", "failed_terminal", "skipped", "deleted"}

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

db_lock = threading.RLock()
settings_lock = threading.RLock()
worker_control_lock = threading.RLock()
current_process = None
shutdown_event = threading.Event()
worker_started_at = time.time()
last_scan_at = 0
last_worker_error = ""

preview_lock = threading.RLock()
preview_status = {
    "running": False,
    "state": "idle",
    "started_at": 0,
    "finished_at": 0,
    "scanned": 0,
    "matched": 0,
    "skipped": 0,
    "current_path": "",
    "message": "Ожидание",
    "result": None,
    "error": ""
}

DEFAULT_SETTINGS = {
    # По умолчанию ничего не запускаем и не выбираем, чтобы приложение не трогало случайные папки.
    "worker_enabled": True,
    "auto_convert_enabled": False,
    "input_path": "",
    "output_path": "",
    "use_failed_path": False,
    "failed_path": "",
    "use_temp_path": False,
    "temp_path": "",
    "preserve_input_folder": True,
    "scan_interval_seconds": 30,
    "stable_check_seconds": 20,
    "stable_check_count": 2,
    "allowed_extensions": "mp4,mkv,mov,avi,m4v,webm",
    "include_pattern": "",
    "exclude_pattern": "",
    "min_duration_seconds": 0,
    "max_duration_seconds": 0,
    "min_size_mb": 0,
    "max_size_mb": 0,
    "filter_min_width": 0,
    "filter_max_width": 0,
    "filter_min_height": 0,
    "filter_max_height": 0,
    "process_codecs": "h264,mpeg4,vp9,av1,unknown",
    "hevc_action": "move",  # skip, move, copy, hardlink
    "max_width": 4096,
    "max_height": 2048,
    "scale_if_too_large": True,
    "video_encoder": "libx265",
    "crf": 24,
    "preset": "medium",
    "pixel_format": "yuv420p",
    "container": "mp4",
    "audio_mode": "aac",  # aac, copy, none
    "audio_bitrate": "160k",
    "copy_metadata": True,
    "copy_chapters": True,
    "faststart": True,
    "ffmpeg_threads": 0,
    "max_retries": 1,
    "move_failed_after_retries": True,
    "delete_source_after_success": False,
    "auto_safe_retry": True,
    "safe_remux_to_mp4": True,
    "allow_mkv_fallback": False,
    "extra_ffmpeg_args": ""
}


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_app(message):
    line = f"{now_iso()} {message}\n"
    try:
        with open(APP_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def load_settings():
    with settings_lock:
        data = dict(DEFAULT_SETTINGS)
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    data.update(saved)
            except Exception as e:
                log_app(f"settings_load_error {e}")
        return data


def save_settings(data):
    with settings_lock:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data or {})
        normalize_settings_inplace(merged)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
        return merged


def normalize_settings_inplace(s):
    for key in ["scan_interval_seconds", "stable_check_seconds", "stable_check_count", "max_width", "max_height", "crf", "ffmpeg_threads", "max_retries", "filter_min_width", "filter_max_width", "filter_min_height", "filter_max_height"]:
        try:
            s[key] = int(s.get(key, DEFAULT_SETTINGS[key]))
        except Exception:
            s[key] = DEFAULT_SETTINGS[key]
    for key in ["min_duration_seconds", "max_duration_seconds", "min_size_mb", "max_size_mb"]:
        try:
            s[key] = float(s.get(key, DEFAULT_SETTINGS[key]))
        except Exception:
            s[key] = DEFAULT_SETTINGS[key]
    for key in ["worker_enabled", "auto_convert_enabled", "use_failed_path", "use_temp_path", "preserve_input_folder", "scale_if_too_large", "copy_metadata", "copy_chapters", "faststart", "move_failed_after_retries", "delete_source_after_success", "auto_safe_retry", "safe_remux_to_mp4", "allow_mkv_fallback"]:
        s[key] = bool(s.get(key, DEFAULT_SETTINGS[key]))


def to_container_path(path):
    """Convert a user-entered path to a path visible inside the container.

    Supported forms:
    - /media/... is the mounted Umbrel external storage.
    - /host/... is the mounted host root.
    - raw host paths like /home/umbrel/... are mapped to /host/home/umbrel/...
      unless they belong to UMBREL_ROOT/external, where /media is preferred.
    """
    if not path:
        return ""
    path = str(path).strip()
    if not path:
        return ""
    if path.startswith(MEDIA_ROOT + "/") or path == MEDIA_ROOT:
        return path
    if path.startswith(HOST_ROOT + "/") or path == HOST_ROOT:
        return path
    if path.startswith(HOST_EXTERNAL_ROOT + "/") or path == HOST_EXTERNAL_ROOT:
        return MEDIA_ROOT + path[len(HOST_EXTERNAL_ROOT):]
    if path.startswith("/"):
        candidate = HOST_ROOT + path
        if os.path.exists(candidate):
            return candidate
    return path


def to_display_path(path):
    return path


def split_csv(value):
    if not value:
        return []
    return [x.strip().lower().lstrip(".") for x in str(value).split(",") if x.strip()]


def safe_rel(path, base):
    path = os.path.abspath(path)
    base = os.path.abspath(base)
    try:
        rel = os.path.relpath(path, base)
    except Exception:
        rel = os.path.basename(path)
    if rel.startswith(".."):
        rel = os.path.basename(path)
    return rel


def ensure_parent(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock:
        conn = db_connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    output_path TEXT,
                    temp_path TEXT,
                    status TEXT NOT NULL,
                    source_codec TEXT,
                    source_width INTEGER,
                    source_height INTEGER,
                    source_duration REAL,
                    source_size_bytes INTEGER DEFAULT 0,
                    output_size_bytes INTEGER DEFAULT 0,
                    progress_percent REAL DEFAULT 0,
                    fps TEXT,
                    speed TEXT,
                    eta_seconds REAL DEFAULT 0,
                    started_at TEXT,
                    finished_at TEXT,
                    elapsed_seconds REAL DEFAULT 0,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    mode TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            conn.commit()
        finally:
            conn.close()


def row_to_dict(row):
    if not row:
        return None
    d = dict(row)
    return d


def upsert_job(source_path, status="pending"):
    with db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE source_path=? ORDER BY created_at DESC LIMIT 1", (source_path,)).fetchone()
            if row:
                return row_to_dict(row)
            jid = uuid.uuid4().hex
            ts = now_iso()
            conn.execute("""
                INSERT INTO jobs(id, source_path, status, created_at, updated_at)
                VALUES(?,?,?,?,?)
            """, (jid, source_path, status, ts, ts))
            conn.commit()
            return row_to_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        finally:
            conn.close()


def update_job(job_id, **fields):
    if not fields:
        return
    fields["updated_at"] = now_iso()
    keys = list(fields.keys())
    sql = "UPDATE jobs SET " + ", ".join([k + "=?" for k in keys]) + " WHERE id=?"
    vals = [fields[k] for k in keys] + [job_id]
    with db_lock:
        conn = db_connect()
        try:
            conn.execute(sql, vals)
            conn.commit()
        finally:
            conn.close()


def get_job(job_id):
    with db_lock:
        conn = db_connect()
        try:
            return row_to_dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        finally:
            conn.close()


def get_existing_job_for_source(src):
    with db_lock:
        conn = db_connect()
        try:
            return row_to_dict(conn.execute("SELECT * FROM jobs WHERE source_path=? ORDER BY created_at DESC LIMIT 1", (src,)).fetchone())
        finally:
            conn.close()


def list_jobs(limit=300, status=None):
    with db_lock:
        conn = db_connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [row_to_dict(r) for r in rows]
        finally:
            conn.close()


def probe_json(path):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]
    try:
        cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
        if cp.returncode != 0:
            return None, cp.stderr.strip()
        return json.loads(cp.stdout), ""
    except Exception as e:
        return None, str(e)


def get_video_info(path):
    data, err = probe_json(path)
    if not data:
        return None, err
    vstream = None
    astreams = []
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and vstream is None:
            vstream = s
        if s.get("codec_type") == "audio":
            astreams.append(s)
    if not vstream:
        return None, "no video stream"
    fmt = data.get("format", {})
    duration = vstream.get("duration") or fmt.get("duration") or 0
    try:
        duration = float(duration)
    except Exception:
        duration = 0.0
    try:
        size = os.path.getsize(path)
    except Exception:
        size = int(float(fmt.get("size", 0) or 0))
    return {
        "codec": str(vstream.get("codec_name") or "unknown"),
        "width": int(vstream.get("width") or 0),
        "height": int(vstream.get("height") or 0),
        "duration": duration,
        "size": size,
        "audio_count": len(astreams)
    }, ""


def is_valid_video(path):
    info, err = get_video_info(path)
    return bool(info and info.get("codec") and info.get("width", 0) > 0)


def file_is_stable(path, settings):
    checks = max(1, int(settings.get("stable_check_count", 2)))
    delay = max(1, int(settings.get("stable_check_seconds", 20)))
    last = -1
    stable = 0
    while not shutdown_event.is_set():
        if not os.path.exists(path):
            return False
        try:
            size = os.path.getsize(path)
        except Exception:
            return False
        if size == last and size > 0:
            stable += 1
        else:
            stable = 0
        last = size
        if stable >= checks:
            return True
        time.sleep(delay)
    return False


def input_base_for_rel(settings):
    input_path = to_container_path(settings.get("input_path", "")).rstrip("/")
    if input_path and os.path.isfile(input_path):
        return os.path.dirname(input_path)
    return input_path


def output_rel_for_source(src, settings):
    input_path = to_container_path(settings.get("input_path", "")).rstrip("/")
    base = input_base_for_rel(settings)
    rel = safe_rel(src, base) if base else os.path.basename(src)
    if settings.get("preserve_input_folder", True) and input_path and os.path.isdir(input_path):
        rel = os.path.basename(input_path) + "/" + rel
    return rel


def compute_output_path(src, settings, info, force_ext=None, suffix=None):
    output_root = to_container_path(settings.get("output_path", "")).rstrip("/")
    rel = output_rel_for_source(src, settings)
    rel_dir = os.path.dirname(rel)
    base = os.path.basename(rel)
    name, ext = os.path.splitext(base)
    too_large = int(info.get("width", 0)) > int(settings.get("max_width", 4096)) or int(info.get("height", 0)) > int(settings.get("max_height", 2048))
    if suffix is None:
        suffix = "_h265"
        if too_large and settings.get("scale_if_too_large", True):
            suffix = f"_{settings.get('max_width',4096)}x{settings.get('max_height',2048)}_h265"
    ext_out = force_ext or "." + str(settings.get("container", "mp4")).lstrip(".")
    return os.path.join(output_root, rel_dir, name + suffix + ext_out)


def compute_passthrough_output_path(src, settings):
    output_root = to_container_path(settings.get("output_path", "")).rstrip("/")
    rel = output_rel_for_source(src, settings)
    return os.path.join(output_root, rel)


def temp_for_output(dst, settings):
    if settings.get("use_temp_path"):
        temp_root = to_container_path(settings.get("temp_path", "")).rstrip("/")
        if temp_root:
            os.makedirs(temp_root, exist_ok=True)
            return os.path.join(temp_root, os.path.basename(dst) + ".tmp")
    return dst + ".tmp" + os.path.splitext(dst)[1]


def should_include_file(path, settings):
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    allowed = split_csv(settings.get("allowed_extensions")) or list(VIDEO_EXTS)
    allowed = [e.lstrip(".").lower() for e in allowed]
    if ext not in allowed:
        return False
    inc = settings.get("include_pattern", "").strip()
    exc = settings.get("exclude_pattern", "").strip()
    if inc and inc.lower() not in path.lower():
        return False
    if exc and exc.lower() in path.lower():
        return False
    return True


def passes_probe_filters(info, settings):
    if not info:
        return False
    dur = float(info.get("duration") or 0)
    size = int(info.get("size") or 0)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    codec = str(info.get("codec") or "unknown").lower()
    min_d = float(settings.get("min_duration_seconds", 0) or 0)
    max_d = float(settings.get("max_duration_seconds", 0) or 0)
    min_mb = float(settings.get("min_size_mb", 0) or 0)
    max_mb = float(settings.get("max_size_mb", 0) or 0)
    min_w = int(settings.get("filter_min_width", 0) or 0)
    max_w = int(settings.get("filter_max_width", 0) or 0)
    min_h = int(settings.get("filter_min_height", 0) or 0)
    max_h = int(settings.get("filter_max_height", 0) or 0)
    if min_d and dur < min_d: return False
    if max_d and dur > max_d: return False
    if min_mb and size < min_mb * 1048576: return False
    if max_mb and size > max_mb * 1048576: return False
    if min_w and width < min_w: return False
    if max_w and width > max_w: return False
    if min_h and height < min_h: return False
    if max_h and height > max_h: return False
    # HEVC is still selectable when action is not skip or when it must be scaled.
    if codec == "hevc":
        too_large = width > int(settings.get("max_width", 4096)) or height > int(settings.get("max_height", 2048))
        return too_large or str(settings.get("hevc_action", "move")) != "skip"
    process_codecs = split_csv(settings.get("process_codecs"))
    return codec in process_codecs or (codec == "unknown" and "unknown" in process_codecs)


def iter_source_files(settings):
    input_path = to_container_path(settings.get("input_path", ""))
    if not input_path:
        return
    outp = os.path.abspath(to_container_path(settings.get("output_path", "") or "/__no_output__"))
    failp = os.path.abspath(to_container_path(settings.get("failed_path", "") or "/__no_failed__"))
    if os.path.isfile(input_path):
        if should_include_file(input_path, settings):
            yield input_path
        return
    if not os.path.isdir(input_path):
        return
    for root, dirs, files in os.walk(input_path):
        absroot = os.path.abspath(root)
        if (outp and (absroot == outp or absroot.startswith(outp + os.sep))) or (failp and (absroot == failp or absroot.startswith(failp + os.sep))):
            dirs[:] = []
            continue
        for name in files:
            p = os.path.join(root, name)
            if should_include_file(p, settings):
                yield p


def iter_all_video_candidates(settings):
    """Scan all known video-like extensions, ignoring selected extension filters.

    This is used for analysis: show everything we can probably process, then let the user
    choose which discovered extensions should be included.
    """
    input_path = to_container_path(settings.get("input_path", ""))
    if not input_path:
        return
    outp = os.path.abspath(to_container_path(settings.get("output_path", "") or "/__no_output__"))
    failp = os.path.abspath(to_container_path(settings.get("failed_path", "") or "/__no_failed__"))
    if os.path.isfile(input_path):
        if os.path.splitext(input_path)[1].lower() in VIDEO_EXTS:
            yield input_path
        return
    if not os.path.isdir(input_path):
        return
    inc = str(settings.get("include_pattern", "")).strip().lower()
    exc = str(settings.get("exclude_pattern", "")).strip().lower()
    for root, dirs, files in os.walk(input_path):
        absroot = os.path.abspath(root)
        if (outp and (absroot == outp or absroot.startswith(outp + os.sep))) or (failp and (absroot == failp or absroot.startswith(failp + os.sep))):
            dirs[:] = []
            continue
        for name in files:
            p = os.path.join(root, name)
            ext = os.path.splitext(p)[1].lower()
            if ext not in VIDEO_EXTS:
                continue
            low = p.lower()
            if inc and inc not in low:
                continue
            if exc and exc in low:
                continue
            yield p


def resolution_bucket(width, height):
    w = int(width or 0); h = int(height or 0)
    long_side = max(w, h); short_side = min(w, h)
    if long_side >= 7000 or short_side >= 3800:
        return "8K"
    if long_side >= 3800 or short_side >= 2000:
        return "4K"
    if long_side >= 2500 or short_side >= 1300:
        return "2K/1440p"
    if long_side >= 1700 or short_side >= 900:
        return "1080p"
    if long_side >= 1100 or short_side >= 600:
        return "720p"
    if long_side > 0:
        return "ниже 720p"
    return "неизвестно"


def add_stat_bucket(dct, key, size):
    key = str(key or "unknown")
    if key not in dct:
        dct[key] = {"name": key, "count": 0, "bytes": 0}
    dct[key]["count"] += 1
    dct[key]["bytes"] += int(size or 0)


def stat_list(dct):
    arr = list(dct.values())
    arr.sort(key=lambda x: (-x.get("count", 0), x.get("name", "")))
    for x in arr:
        x["bytes_h"] = fmt_bytes(x.get("bytes", 0))
    return arr



def set_preview_status(**kwargs):
    with preview_lock:
        preview_status.update(kwargs)
        return dict(preview_status)


def get_preview_status():
    with preview_lock:
        data = dict(preview_status)
        if data.get("started_at") and data.get("running"):
            data["elapsed_seconds"] = round(time.time() - float(data.get("started_at") or 0), 1)
        elif data.get("started_at") and data.get("finished_at"):
            data["elapsed_seconds"] = round(float(data.get("finished_at") or 0) - float(data.get("started_at") or 0), 1)
        else:
            data["elapsed_seconds"] = 0
        return data


def start_preview_analysis(settings):
    with preview_lock:
        if preview_status.get("running"):
            return {"ok": True, "already_running": True, "status": dict(preview_status)}
        preview_status.update({
            "running": True,
            "state": "starting",
            "started_at": time.time(),
            "finished_at": 0,
            "scanned": 0,
            "matched": 0,
            "skipped": 0,
            "current_path": "",
            "message": "Запускаю анализ...",
            "result": None,
            "error": ""
        })

    def runner():
        try:
            result = preview_payload(settings, use_progress=True)
            with preview_lock:
                preview_status.update({
                    "running": False,
                    "state": "done" if result.get("ok") else "error",
                    "finished_at": time.time(),
                    "message": "Сканирование завершено" if result.get("ok") else result.get("error", "Ошибка сканирования"),
                    "result": result,
                    "error": "" if result.get("ok") else result.get("error", "Ошибка сканирования"),
                    "scanned": result.get("scanned", preview_status.get("scanned", 0)),
                    "matched": result.get("matched", preview_status.get("matched", 0)),
                    "skipped": result.get("skipped", preview_status.get("skipped", 0)),
                    "current_path": ""
                })
        except Exception as e:
            log_app(f"preview_thread_error {repr(e)}")
            with preview_lock:
                preview_status.update({
                    "running": False,
                    "state": "error",
                    "finished_at": time.time(),
                    "message": "Ошибка сканирования: " + str(e),
                    "error": str(e),
                    "current_path": ""
                })

    threading.Thread(target=runner, daemon=True).start()
    return {"ok": True, "already_running": False, "status": get_preview_status()}


def preview_payload(settings, use_progress=False):
    settings = dict(load_settings(), **(settings or {}))
    normalize_settings_inplace(settings)
    scanned = 0
    matched = 0
    skipped = 0
    total_bytes = 0
    all_bytes = 0
    samples = []
    warnings = []
    ext_stats = {}
    codec_stats = {}
    res_stats = {}
    matched_ext_stats = {}
    matched_codec_stats = {}
    raw_input_path = str(settings.get("input_path", "") or "")
    input_path = to_container_path(raw_input_path)
    empty_result = {"ok": False, "error": "", "scanned": 0, "matched": 0, "skipped": 0, "source_bytes": 0, "source_h": fmt_bytes(0), "all_source_h": fmt_bytes(0), "all_source_bytes": 0, "samples": [], "warnings": [], "ext_stats": [], "codec_stats": [], "resolution_stats": [], "matched_ext_stats": [], "matched_codec_stats": [], "input_path": raw_input_path, "container_input_path": input_path}
    if not input_path:
        empty_result["error"] = "Не выбран входящий файл или папка"
        if use_progress:
            set_preview_status(running=False, state="error", message=empty_result["error"], error=empty_result["error"], result=empty_result, finished_at=time.time())
        return empty_result
    if not os.path.exists(input_path):
        empty_result["error"] = "Входящий путь не существует внутри контейнера: " + input_path
        empty_result["warnings"].append("Выбранный путь: " + raw_input_path)
        empty_result["warnings"].append("Путь внутри контейнера: " + input_path)
        if use_progress:
            set_preview_status(running=False, state="error", message=empty_result["error"], error=empty_result["error"], result=empty_result, finished_at=time.time())
        return empty_result
    if use_progress:
        set_preview_status(running=True, state="scanning", scanned=0, matched=0, skipped=0, current_path=input_path, message="Сканирую: " + input_path)
    limit = int(settings.get("preview_probe_limit", 3000) or 3000)
    last_progress_update = time.time()
    allowed = split_csv(settings.get("allowed_extensions")) or [e.lstrip('.') for e in VIDEO_EXTS]
    allowed = set(x.lower().lstrip('.') for x in allowed)
    for p in iter_all_video_candidates(settings):
        scanned += 1
        if use_progress and (scanned == 1 or scanned % 10 == 0 or time.time() - last_progress_update > 0.7):
            set_preview_status(running=True, state="scanning", scanned=scanned, matched=matched, skipped=skipped, current_path=p, message=f"Сканирую файл {scanned}: {os.path.basename(p)}")
            last_progress_update = time.time()
        if scanned > limit:
            warnings.append(f"Предпросмотр остановлен на лимите {limit} файлов")
            break
        ext = os.path.splitext(p)[1].lower().lstrip('.') or "unknown"
        try:
            fsize = os.path.getsize(p)
        except Exception:
            fsize = 0
        all_bytes += fsize
        add_stat_bucket(ext_stats, ext, fsize)
        if ext not in allowed:
            skipped += 1
            continue
        info, err = get_video_info(p)
        if info:
            add_stat_bucket(codec_stats, info.get("codec"), info.get("size") or fsize)
            add_stat_bucket(res_stats, resolution_bucket(info.get("width"), info.get("height")), info.get("size") or fsize)
        if info and passes_probe_filters(info, settings):
            matched += 1
            total_bytes += int(info.get("size") or fsize)
            add_stat_bucket(matched_ext_stats, ext, info.get("size") or fsize)
            add_stat_bucket(matched_codec_stats, info.get("codec"), info.get("size") or fsize)
            if len(samples) < 30:
                samples.append({"path": p, "ext": ext, "codec": info.get("codec"), "width": info.get("width"), "height": info.get("height"), "duration": round(float(info.get("duration") or 0), 1), "size_h": fmt_bytes(info.get("size") or fsize)})
        else:
            skipped += 1
    result = {"ok": True, "scanned": scanned, "matched": matched, "skipped": skipped, "source_bytes": total_bytes, "source_h": fmt_bytes(total_bytes), "all_source_bytes": all_bytes, "all_source_h": fmt_bytes(all_bytes), "samples": samples, "warnings": warnings, "ext_stats": stat_list(ext_stats), "codec_stats": stat_list(codec_stats), "resolution_stats": stat_list(res_stats), "matched_ext_stats": stat_list(matched_ext_stats), "matched_codec_stats": stat_list(matched_codec_stats), "input_path": raw_input_path, "container_input_path": input_path, "is_file": os.path.isfile(input_path), "is_dir": os.path.isdir(input_path)}
    if use_progress:
        set_preview_status(running=False, state="done", scanned=scanned, matched=matched, skipped=skipped, current_path="", message="Сканирование завершено", result=result, finished_at=time.time())
    return result


def list_path(path):
    if not path:
        roots = [
            {"name": "Внешние диски Umbrel (/media)", "path": MEDIA_ROOT, "is_dir": True, "type": "dir"},
            {"name": "Вся система хоста (/host)", "path": HOST_ROOT, "is_dir": True, "type": "dir"},
            {"name": "Данные приложения (/data)", "path": DATA_DIR, "is_dir": True, "type": "dir"},
        ]
        return {"path": "", "parent": "", "entries": roots}
    path = to_container_path(unquote(path))
    if os.path.isfile(path):
        parent = os.path.dirname(path)
        return {"path": path, "parent": parent, "entries": []}
    if not os.path.isdir(path):
        return {"path": path, "parent": os.path.dirname(path), "entries": [], "error": "Папка не найдена или нет доступа"}
    entries = []
    try:
        names = sorted(os.listdir(path), key=lambda n: (not os.path.isdir(os.path.join(path, n)), n.lower()))
        for name in names[:1000]:
            full = os.path.join(path, name)
            try:
                is_dir = os.path.isdir(full)
                is_file = os.path.isfile(full)
                size = os.path.getsize(full) if is_file else 0
            except Exception:
                continue
            if is_dir or is_file:
                entries.append({"name": name, "path": full, "is_dir": is_dir, "is_file": is_file, "type": "dir" if is_dir else "file", "size_h": fmt_bytes(size)})
    except Exception as e:
        return {"path": path, "parent": os.path.dirname(path), "entries": [], "error": str(e)}
    return {"path": path, "parent": os.path.dirname(path.rstrip('/')) if path not in ('/', HOST_ROOT, MEDIA_ROOT) else "", "entries": entries}


def scan_files():
    global last_scan_at
    settings = load_settings()
    input_path = to_container_path(settings.get("input_path", ""))
    output_path = to_container_path(settings.get("output_path", ""))
    if not input_path or not os.path.exists(input_path) or not output_path:
        last_scan_at = time.time()
        return 0
    count = 0
    for p in iter_source_files(settings):
        existing = get_existing_job_for_source(p)
        if existing and existing.get("status") in TERMINAL_STATUSES:
            continue
        if existing and existing.get("status") in {"pending", "waiting_file_copy", "probing", "converting"}:
            continue
        upsert_job(p)
        count += 1
    last_scan_at = time.time()
    return count


def pick_next_job():
    with db_lock:
        conn = db_connect()
        try:
            row = conn.execute("""
                SELECT * FROM jobs
                WHERE status IN ('pending','failed')
                ORDER BY created_at ASC
                LIMIT 1
            """).fetchone()
            return row_to_dict(row)
        finally:
            conn.close()


def build_ffmpeg_cmd(src, tmp, settings, info, safe=False, no_audio=False):
    max_w = int(settings.get("max_width", 4096))
    max_h = int(settings.get("max_height", 2048))
    too_large = int(info.get("width", 0)) > max_w or int(info.get("height", 0)) > max_h
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y"]
    if safe:
        cmd += ["-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err"]
    cmd += ["-i", src]
    if no_audio or settings.get("audio_mode") == "none":
        cmd += ["-map", "0:v:0", "-an"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a?"]
    if settings.get("copy_metadata", True) and not safe:
        cmd += ["-map_metadata", "0"]
    else:
        cmd += ["-map_metadata", "-1"]
    if settings.get("copy_chapters", True) and not safe:
        cmd += ["-map_chapters", "0"]
    else:
        cmd += ["-map_chapters", "-1"]
    vf = "format=" + str(settings.get("pixel_format", "yuv420p"))
    if too_large and settings.get("scale_if_too_large", True):
        vf = f"scale='min({max_w},iw)':'min({max_h},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2," + vf
    if safe:
        vf = "fps=30000/1001," + vf
    cmd += ["-vf", vf]
    if safe and not no_audio and settings.get("audio_mode") != "none":
        cmd += ["-af", "aresample=async=1000:first_pts=0"]
    cmd += ["-c:v", str(settings.get("video_encoder", "libx265"))]
    threads = int(settings.get("ffmpeg_threads", 0) or 0)
    if threads > 0:
        cmd += ["-threads", str(threads)]
    cmd += ["-preset", str(settings.get("preset", "medium")), "-crf", str(settings.get("crf", 24)), "-pix_fmt", str(settings.get("pixel_format", "yuv420p"))]
    if not no_audio and settings.get("audio_mode") != "none":
        if settings.get("audio_mode") == "copy" and not safe:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", str(settings.get("audio_bitrate", "160k"))]
    if os.path.splitext(tmp)[1].lower() == ".mp4" and settings.get("faststart", True):
        cmd += ["-movflags", "+faststart"]
    extra = str(settings.get("extra_ffmpeg_args", "")).strip()
    if extra:
        # intentionally simple split; advanced user field
        cmd += extra.split()
    cmd += ["-progress", "pipe:1", "-nostats", tmp]
    return cmd


def run_ffmpeg_with_progress(job, cmd, duration):
    global current_process
    start = time.time()
    progress = {"out_time_ms": 0.0, "fps": "", "speed": ""}
    with open(FFMPEG_LOG, "a", encoding="utf-8", errors="replace") as logf:
        logf.write("\n" + "="*80 + "\n")
        logf.write(now_iso() + " JOB " + job["id"] + "\n")
        logf.write("CMD: " + json.dumps(cmd, ensure_ascii=False) + "\n")
        logf.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=logf, text=True, bufsize=1)
        with worker_control_lock:
            current_process = proc
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k in progress:
                    progress[k] = v
                if k == "out_time_ms":
                    try:
                        progress[k] = float(v)
                    except Exception:
                        progress[k] = 0.0
                if k in {"out_time_ms", "fps", "speed", "progress"}:
                    out_ms = float(progress.get("out_time_ms") or 0)
                    pct = 0.0
                    if duration and duration > 0:
                        pct = min(100.0, (out_ms / 1000000.0) / duration * 100.0)
                    elapsed = time.time() - start
                    eta = 0.0
                    if pct > 0 and pct < 100:
                        eta = elapsed * (100.0 - pct) / pct
                    update_job(job["id"], progress_percent=round(pct, 2), fps=str(progress.get("fps") or ""), speed=str(progress.get("speed") or ""), eta_seconds=round(eta, 1), elapsed_seconds=round(elapsed, 1))
            rc = proc.wait()
            return rc, time.time() - start
        finally:
            with worker_control_lock:
                current_process = None


def remux_mkv_to_mp4(mkv, mp4):
    ensure_parent(mp4)
    tmp = mp4 + ".tmp.mp4"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-i", mkv, "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-tag:v", "hvc1", "-movflags", "+faststart", tmp]
    with open(FFMPEG_LOG, "a", encoding="utf-8", errors="replace") as logf:
        logf.write("\nREMUX CMD: " + json.dumps(cmd, ensure_ascii=False) + "\n")
        cp = subprocess.run(cmd, stdout=logf, stderr=logf, text=True)
    if cp.returncode == 0 and is_valid_video(tmp):
        os.replace(tmp, mp4)
        return True, ""
    try:
        os.remove(tmp)
    except Exception:
        pass
    return False, f"remux failed rc={cp.returncode}"


def move_to_failed(job, settings, reason):
    src = job["source_path"]
    if not settings.get("use_failed_path", False):
        update_job(job["id"], status="failed_terminal", error_message=reason + "; карантин выключен", finished_at=now_iso())
        return
    failed_root = to_container_path(settings.get("failed_path", ""))
    if not failed_root:
        update_job(job["id"], status="failed_terminal", error_message=reason + "; папка карантина не выбрана", finished_at=now_iso())
        return
    base = input_base_for_rel(settings)
    rel = safe_rel(src, base) if base else os.path.basename(src)
    dst = os.path.join(failed_root, rel)
    try:
        ensure_parent(dst)
        if os.path.exists(src):
            shutil.move(src, dst)
        update_job(job["id"], status="moved_to_failed", output_path=dst, error_message=reason, finished_at=now_iso())
    except Exception as e:
        update_job(job["id"], status="failed_terminal", error_message=f"failed move to quarantine: {e}; original error: {reason}", finished_at=now_iso())


def process_job(job):
    settings = load_settings()
    src = job["source_path"]
    if not to_container_path(settings.get("output_path", "")):
        update_job(job["id"], status="failed_terminal", error_message="Не выбрана выходная папка", finished_at=now_iso())
        return
    if not os.path.exists(src):
        update_job(job["id"], status="skipped", error_message="source no longer exists", finished_at=now_iso())
        return
    update_job(job["id"], status="waiting_file_copy", started_at=now_iso(), progress_percent=0)
    if not file_is_stable(src, settings):
        update_job(job["id"], status="skipped", error_message="source disappeared during stability check", finished_at=now_iso())
        return
    update_job(job["id"], status="probing")
    info, err = get_video_info(src)
    if not info:
        update_job(job["id"], status="skipped", error_message="ffprobe failed: " + err, finished_at=now_iso())
        return
    dur = float(info.get("duration") or 0)
    size = int(info.get("size") or 0)
    if not passes_probe_filters(info, settings):
        update_job(job["id"], status="skipped", source_codec=info["codec"], source_width=info["width"], source_height=info["height"], source_duration=dur, source_size_bytes=size, error_message="Не подходит под фильтры", finished_at=now_iso())
        return
    update_job(job["id"], source_codec=info["codec"], source_width=info["width"], source_height=info["height"], source_duration=dur, source_size_bytes=size)
    codec = info["codec"].lower()
    too_large = info["width"] > int(settings.get("max_width", 4096)) or info["height"] > int(settings.get("max_height", 2048))
    # already hevc and no scale required
    if codec == "hevc" and not too_large:
        action = str(settings.get("hevc_action", "move"))
        dst = compute_passthrough_output_path(src, settings)
        ensure_parent(dst)
        try:
            if os.path.exists(dst) and is_valid_video(dst):
                update_job(job["id"], status="skip_ok", output_path=dst, output_size_bytes=os.path.getsize(dst), progress_percent=100, finished_at=now_iso(), mode="already_hevc_exists")
                return
            if action == "move":
                shutil.move(src, dst)
                status = "moved_already_hevc"
            elif action == "copy":
                shutil.copy2(src, dst)
                status = "copied_already_hevc"
            elif action == "hardlink":
                os.link(src, dst)
                status = "hardlinked_already_hevc"
            else:
                update_job(job["id"], status="skipped", output_path="", output_size_bytes=0, progress_percent=100, finished_at=now_iso(), mode="already_hevc_skip")
                return
            update_job(job["id"], status=status, output_path=dst, output_size_bytes=os.path.getsize(dst), progress_percent=100, finished_at=now_iso(), mode="already_hevc")
            return
        except Exception as e:
            update_job(job["id"], status="failed", error_message=str(e), retry_count=int(job.get("retry_count") or 0)+1, finished_at=now_iso())
            return
    process_codecs = split_csv(settings.get("process_codecs"))
    if codec not in process_codecs and "unknown" not in process_codecs:
        update_job(job["id"], status="skipped", error_message="codec not selected: " + codec, finished_at=now_iso())
        return
    dst = compute_output_path(src, settings, info)
    tmp = temp_for_output(dst, settings)
    ensure_parent(dst)
    ensure_parent(tmp)
    if os.path.exists(dst) and is_valid_video(dst):
        update_job(job["id"], status="skip_ok", output_path=dst, output_size_bytes=os.path.getsize(dst), progress_percent=100, finished_at=now_iso(), mode="output_exists")
        return
    try:
        os.remove(tmp)
    except Exception:
        pass
    update_job(job["id"], status="converting", output_path=dst, temp_path=tmp, progress_percent=0, error_message="", mode="normal")
    cmd = build_ffmpeg_cmd(src, tmp, settings, info, safe=False)
    rc, elapsed = run_ffmpeg_with_progress(job, cmd, dur)
    if rc == 0 and is_valid_video(tmp):
        os.replace(tmp, dst)
        out_size = os.path.getsize(dst)
        if settings.get("delete_source_after_success", False):
            try:
                os.remove(src)
            except Exception as e:
                log_app(f"delete_source_failed {src} {e}")
        update_job(job["id"], status="success", output_size_bytes=out_size, progress_percent=100, elapsed_seconds=round(elapsed,1), finished_at=now_iso(), mode="normal")
        return
    try:
        os.remove(tmp)
    except Exception:
        pass
    # Safe retry: make MKV, then remux to MP4
    if settings.get("auto_safe_retry", True):
        safe_mkv = os.path.splitext(dst)[0] + "_SAFE_TMP.mkv"
        try:
            os.remove(safe_mkv)
        except Exception:
            pass
        update_job(job["id"], status="converting", progress_percent=0, mode="safe_mkv", error_message="normal failed, trying safe mkv")
        cmd2 = build_ffmpeg_cmd(src, safe_mkv, settings, info, safe=True)
        rc2, elapsed2 = run_ffmpeg_with_progress(job, cmd2, dur)
        if rc2 == 0 and is_valid_video(safe_mkv):
            final_ok = False
            if settings.get("safe_remux_to_mp4", True) and os.path.splitext(dst)[1].lower() == ".mp4":
                ok, remux_err = remux_mkv_to_mp4(safe_mkv, dst)
                if ok:
                    final_ok = True
                    try:
                        os.remove(safe_mkv)
                    except Exception:
                        pass
                else:
                    log_app(remux_err)
            if not final_ok and settings.get("allow_mkv_fallback", False):
                dst_mkv = os.path.splitext(dst)[0] + ".mkv"
                os.replace(safe_mkv, dst_mkv)
                dst = dst_mkv
                final_ok = True
            if final_ok and is_valid_video(dst):
                out_size = os.path.getsize(dst)
                if settings.get("delete_source_after_success", False):
                    try:
                        os.remove(src)
                    except Exception as e:
                        log_app(f"delete_source_failed {src} {e}")
                update_job(job["id"], status="success", output_path=dst, output_size_bytes=out_size, progress_percent=100, elapsed_seconds=round(elapsed+elapsed2,1), finished_at=now_iso(), mode="safe_fixed")
                return
            try:
                os.remove(safe_mkv)
            except Exception:
                pass
    retry = int(job.get("retry_count") or 0) + 1
    update_job(job["id"], status="failed", retry_count=retry, error_message=f"ffmpeg failed rc={rc}", elapsed_seconds=round(elapsed,1), finished_at=now_iso())
    if retry >= int(settings.get("max_retries", 1)) and settings.get("move_failed_after_retries", True):
        job2 = get_job(job["id"])
        move_to_failed(job2, settings, f"ffmpeg failed after {retry} attempts")


def worker_loop():
    global last_worker_error
    log_app("worker_loop_started")
    while not shutdown_event.is_set():
        try:
            settings = load_settings()
            if not settings.get("worker_enabled", True) or not settings.get("auto_convert_enabled", True):
                time.sleep(2)
                continue
            scan_files()
            job = pick_next_job()
            if job:
                process_job(job)
            else:
                time.sleep(max(2, int(settings.get("scan_interval_seconds", 30))))
        except Exception as e:
            last_worker_error = str(e)
            log_app("worker_error " + repr(e))
            time.sleep(5)


def fmt_bytes(n):
    try:
        n = float(n)
    except Exception:
        n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units)-1:
        n /= 1024.0
        i += 1
    if i == 0:
        return f"{int(n)}{units[i]}"
    return f"{n:.1f}{units[i]}"


def clear_jobs_and_logs():
    with db_lock:
        conn = db_connect()
        try:
            conn.execute("DELETE FROM jobs")
            conn.commit()
        finally:
            conn.close()
    for file in (FFMPEG_LOG, APP_LOG):
        try:
            open(file, "w", encoding="utf-8").close()
        except Exception:
            pass
    return True


def reset_settings_to_defaults():
    try:
        if os.path.exists(SETTINGS_PATH):
            os.remove(SETTINGS_PATH)
    except Exception as e:
        log_app(f"settings_reset_error {e}")
    return save_settings(DEFAULT_SETTINGS)


def stats_payload():
    with db_lock:
        conn = db_connect()
        try:
            rows = conn.execute("SELECT * FROM jobs").fetchall()
        finally:
            conn.close()
    total = len(rows)
    done = 0
    pending = 0
    failed = 0
    converting = 0
    orig = 0
    out = 0
    elapsed = 0
    current = None
    status_counts = {}
    for r in rows:
        st = r["status"]
        status_counts[st] = status_counts.get(st, 0) + 1
        if st in SUCCESS_STATUSES:
            done += 1
            orig += int(r["source_size_bytes"] or 0)
            out += int(r["output_size_bytes"] or 0)
            elapsed += float(r["elapsed_seconds"] or 0)
        elif st in {"pending", "waiting_file_copy", "probing"}:
            pending += 1
        elif st == "converting":
            converting += 1
            current = row_to_dict(r)
        elif st in {"failed", "failed_terminal", "moved_to_failed"}:
            failed += 1
    active_job = current
    saved = max(0, orig - out)
    pct = (saved / orig * 100) if orig else 0
    return {
        "version": APP_VERSION,
        "worker_enabled": load_settings().get("worker_enabled"),
        "auto_convert_enabled": load_settings().get("auto_convert_enabled"),
        "total": total,
        "done": done,
        "pending": pending,
        "failed": failed,
        "converting": converting,
        "progress_percent": round(done / total * 100, 2) if total else 100.0,
        "source_bytes": orig,
        "output_bytes": out,
        "saved_bytes": saved,
        "saved_percent": round(pct, 2),
        "source_h": fmt_bytes(orig),
        "output_h": fmt_bytes(out),
        "saved_h": fmt_bytes(saved),
        "elapsed_seconds": elapsed,
        "elapsed_h": "%02d:%02d:%02d" % (int(elapsed)//3600, (int(elapsed)%3600)//60, int(elapsed)%60),
        "current": active_job,
        "status_counts": status_counts,
        "last_scan_at": last_scan_at,
        "worker_uptime_seconds": time.time() - worker_started_at,
        "last_worker_error": last_worker_error
    }

INDEX_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Авто H.265 Конвертер</title>
  <style>
    :root{--bg:#071013;--panel:#101b1f;--panel2:#16262b;--soft:#0b171b;--text:#e8f2f2;--muted:#96a6a8;--cyan:#22d3ee;--green:#22c55e;--yellow:#f59e0b;--red:#ef4444;--border:#26373d;--blue:#60a5fa;--violet:#a78bfa;--pink:#f472b6;--orange:#fb923c}
    *{box-sizing:border-box} body{margin:0;background:radial-gradient(circle at top,#12343b,#071013 45%);font-family:Inter,system-ui,Segoe UI,Arial,sans-serif;color:var(--text)}
    header{padding:22px 28px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:rgba(8,17,20,.78);backdrop-filter:blur(8px);position:sticky;top:0;z-index:5}
    h1{font-size:22px;margin:0}.tag{font-size:12px;color:var(--muted)}main{padding:24px;max-width:1680px;margin:0 auto}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.card{background:linear-gradient(180deg,var(--panel),#0d171a);border:1px solid var(--border);border-radius:18px;padding:18px;box-shadow:0 10px 30px rgba(0,0,0,.18)}
    .card h3{margin:0 0 10px;color:var(--muted);font-weight:700;font-size:13px;text-transform:uppercase;letter-spacing:.08em}.card h2{font-size:18px;margin:0 0 14px}.big{font-size:30px;font-weight:800}.small{font-size:13px;color:var(--muted)}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--yellow)}.blue{color:var(--blue)}.cyan{color:var(--cyan)}
    .bar{height:12px;background:#071013;border:1px solid var(--border);border-radius:999px;overflow:hidden}.bar>div{height:100%;background:linear-gradient(90deg,var(--cyan),var(--green));width:0%}.section{margin-top:18px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.btn{background:var(--panel2);color:var(--text);border:1px solid var(--border);border-radius:12px;padding:9px 13px;cursor:pointer;font-weight:700}.btn:hover{border-color:var(--cyan)}.btn.green{background:#0f2d1b;border-color:#166534}.btn.red{background:#321316;border-color:#7f1d1d}.btn.yellow{background:#2f250c;border-color:#854d0e}.btn.blue{background:#10233b;border-color:#1d4ed8}.btn:disabled{opacity:.45;cursor:not-allowed}
    input,select,textarea{width:100%;background:#071013;color:var(--text);border:1px solid var(--border);border-radius:10px;padding:9px}textarea{min-height:70px}input:disabled,select:disabled{opacity:.5;background:#0b1113}label{font-size:12px;color:var(--muted);display:flex;gap:7px;align-items:center;margin-bottom:5px}.q{display:inline-flex;align-items:center;justify-content:center;width:17px;height:17px;border-radius:50%;border:1px solid var(--border);color:var(--cyan);font-size:12px;cursor:help}.q:hover::after{content:attr(data-tip);position:absolute;z-index:50;max-width:360px;transform:translate(10px,24px);background:#071013;color:var(--text);border:1px solid var(--cyan);border-radius:12px;padding:10px;box-shadow:0 12px 30px rgba(0,0,0,.45);line-height:1.35}.formgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.span2{grid-column:span 2}.span3{grid-column:span 3}.span4{grid-column:span 4}.hidden{display:none}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.muted{color:var(--muted)}
    table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px;border-bottom:1px solid var(--border);vertical-align:top}th{text-align:left;color:var(--muted);font-size:12px}td.path{max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.pill{padding:3px 8px;border-radius:999px;background:#102126;border:1px solid var(--border);font-size:12px}.log{white-space:pre-wrap;background:#071013;border:1px solid var(--border);border-radius:12px;padding:12px;max-height:420px;overflow:auto;color:#cbd5e1}.hint{border-left:3px solid var(--cyan);padding:10px 12px;background:#09181c;border-radius:10px;color:#b8c6c8}
    .preset{display:flex;flex-direction:column;gap:4px;align-items:flex-start;min-width:210px}.preset b{font-size:14px}.preset span{font-size:12px;color:var(--muted);font-weight:500}.danger{border-color:#7f1d1d!important}.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:20;display:none;align-items:center;justify-content:center}.modal.show{display:flex}.modalbox{width:min(980px,92vw);max-height:84vh;background:#0d171a;border:1px solid var(--border);border-radius:18px;padding:16px;box-shadow:0 20px 80px rgba(0,0,0,.45)}.filelist{max-height:54vh;overflow:auto;border:1px solid var(--border);border-radius:12px}.fileitem{display:grid;grid-template-columns:32px 1fr 110px 110px;gap:8px;padding:9px 12px;border-bottom:1px solid #1c2b31;cursor:pointer}.fileitem:hover{background:#122329}.pie{width:230px;height:230px;border-radius:50%;border:1px solid var(--border);background:#102126;margin:auto}.legend{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.legendItem{display:flex;gap:8px;align-items:center;font-size:13px}.sw{width:14px;height:14px;border-radius:3px}.checkgrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.check{display:flex;align-items:center;gap:8px;background:#071013;border:1px solid var(--border);border-radius:10px;padding:8px}.check input{width:auto}.details{border:1px solid var(--border);border-radius:14px;padding:12px;background:#0b171b}.details summary{cursor:pointer;font-weight:800;color:var(--cyan)}
    @media(max-width:1000px){.grid,.grid2,.grid3{grid-template-columns:1fr 1fr}.formgrid{grid-template-columns:1fr}.span2,.span3,.span4{grid-column:span 1}.checkgrid{grid-template-columns:1fr 1fr}}@media(max-width:640px){.grid,.grid2,.grid3{grid-template-columns:1fr}main{padding:14px}header{padding:16px}.fileitem{grid-template-columns:28px 1fr}.fileitem .muted{display:none}.legend{grid-template-columns:1fr}.checkgrid{grid-template-columns:1fr}}
  </style>
</head>
<body>
<header><div><h1>Авто H.265 Конвертер</h1><div class="tag">Umbrel · FFmpeg · H.264 → H.265 · живой прогресс</div></div><div class="row"><button class="btn" onclick="showTab('dashboard')">Главная</button><button class="btn" onclick="showTab('settings')">Настройки</button><button class="btn" onclick="showTab('jobs')">Очередь</button><button class="btn" onclick="showTab('logs')">Логи</button></div></header>
<main>
<section id="dashboard">
  <div class="grid"><div class="card"><h3>Общий прогресс</h3><div class="big" id="progressText">0 / 0</div><div class="bar"><div id="progressBar"></div></div><div class="small" id="progressPercent">0%</div></div><div class="card"><h3>Экономия</h3><div class="big ok" id="savedText">0B</div><div class="small" id="savedPercent">0%</div></div><div class="card"><h3>Объём</h3><div class="big" id="volumeText">0B → 0B</div><div class="small">исходники → результат</div></div><div class="card"><h3>Ошибки</h3><div class="big" id="failedText">0</div><div class="small" id="serviceText">сервис</div></div></div>
  <div class="card section"><h3>Текущий файл</h3><div id="currentBox" class="small">Ожидание</div><div class="bar" style="margin-top:12px"><div id="currentBar"></div></div></div>
  <div class="card section"><h3>Управление</h3><div class="row"><button class="btn green" onclick="apiPost('/api/worker/resume')">Включить автоконвертацию</button><button class="btn yellow" onclick="apiPost('/api/worker/pause')">Пауза</button><button class="btn" onclick="apiPost('/api/scan')">Сканировать сейчас</button><button class="btn red" onclick="apiPost('/api/worker/stop-current')">Остановить текущий FFmpeg</button></div></div>
</section>
<section id="settings" class="hidden">
  <div class="card"><h2>Быстрые пресеты</h2><div class="hint">Пресет просто заполняет настройки. Потом можно руками поменять любой параметр.</div><div class="row" style="margin-top:12px" id="presetButtons"></div></div>
  <div class="card section"><h2>1. Что обрабатывать и куда складывать</h2><div class="formgrid" id="pathForm"></div></div>
  <div class="card section"><h2>2. Предпросмотр найденных видео</h2><div class="row"><button class="btn blue" onclick="previewFilters()">Проанализировать папку / файл</button><button class="btn" onclick="selectAllExt(true)">Выбрать все форматы</button><button class="btn" onclick="selectAllExt(false)">Снять все</button></div><div class="grid2" style="margin-top:14px"><div><div id="pie" class="pie"></div></div><div><h3>Форматы, найденные при сканировании</h3><div id="extChecks" class="checkgrid"></div><div id="previewSummary" class="log mono" style="margin-top:12px;max-height:220px">Нажми “Проанализировать”.</div></div></div></div>
  <div class="card section"><h2>3. Простые настройки конвертации</h2><div class="formgrid" id="simpleForm"></div><div class="row" style="margin-top:12px"><button class="btn" onclick="setResolutionPreset('720p')">Выход до 720p</button><button class="btn" onclick="setResolutionPreset('1080p')">до 1080p</button><button class="btn" onclick="setResolutionPreset('2k')">до 2K</button><button class="btn" onclick="setResolutionPreset('4k')">до 4K</button><button class="btn" onclick="setResolutionPreset('4096x2048')">до 4096×2048</button></div></div>
  <div class="card section"><details class="details"><summary>Расширенные настройки FFmpeg и поведения при ошибках</summary><div class="formgrid" id="advancedForm" style="margin-top:14px"></div></details></div>
  <div class="card section"><h2>Сохранение и обслуживание</h2><div class="row"><button class="btn green" onclick="saveSettings()">Сохранить настройки</button><button class="btn" onclick="loadSettings()">Перезагрузить форму</button><button class="btn yellow" onclick="clearPaths()">Очистить пути</button><button class="btn yellow" onclick="resetSettings()">Сбросить настройки</button><button class="btn red" onclick="clearStats()">Очистить статистику/очередь</button></div><div class="small" style="margin-top:10px">Очистка статистики удаляет историю сканирования и очередь из базы приложения. Готовые видео и исходники не удаляются.</div></div>
</section>
<section id="jobs" class="hidden"><div class="card"><h3>Очередь</h3><div class="row"><button class="btn" onclick="loadJobs()">Обновить</button><select id="jobStatus" style="max-width:240px" onchange="loadJobs()"><option value="">все</option><option value="pending">ожидает</option><option value="converting">конвертируется</option><option value="success">готово</option><option value="failed">ошибка</option><option value="failed_terminal">ошибка без повтора</option><option value="moved_to_failed">в карантине</option><option value="skipped">пропущено</option></select></div><div style="overflow:auto;margin-top:12px"><table><thead><tr><th>Статус</th><th>Прогресс</th><th>Источник</th><th>Результат</th><th>Размер</th><th>Ошибка</th><th>Действия</th></tr></thead><tbody id="jobsBody"></tbody></table></div></div></section>
<section id="logs" class="hidden"><div class="card"><h3>Логи</h3><div class="row"><button class="btn" onclick="loadLogs('app')">Лог приложения</button><button class="btn" onclick="loadLogs('ffmpeg')">Лог FFmpeg</button></div><div class="log mono" id="logBox">...</div></div></section>
</main>
<div id="browserModal" class="modal"><div class="modalbox"><div class="row" style="justify-content:space-between"><h3 id="browserTitle">Выбор пути</h3><button class="btn red" onclick="closeBrowser()">Закрыть</button></div><div class="row"><input id="browserPath" class="mono" placeholder="/media или /host"><button class="btn" onclick="browseTo(document.getElementById('browserPath').value)">Открыть</button><button class="btn" onclick="selectCurrentFolder()">Выбрать эту папку</button></div><div class="filelist" id="browserList" style="margin-top:12px"></div></div></div>
<script>
let settingsCache={}, browserTarget=null, browserMode='dir', browserCurrent='', previewData=null;
const colors=['#22d3ee','#22c55e','#f59e0b','#ef4444','#60a5fa','#a78bfa','#f472b6','#fb923c','#84cc16','#14b8a6','#e879f9','#facc15'];
const tips={input_path:'Файл или папка, откуда приложение будет брать видео. Можно выбрать /media для внешних дисков Umbrel или /host для всей системы.',output_path:'Папка, куда складывать готовые файлы. Лучше выбирать отдельную папку, чтобы не смешивать исходники и результат.',auto_convert_enabled:'Если выключено, приложение не начнёт конвертировать автоматически. Можно только сканировать и смотреть предпросмотр.',worker_enabled:'Фоновый обработчик. Обычно должен быть включён. Если выключить, очередь не будет выполняться.',allowed_extensions:'Форматы файлов, которые будут реально взяты в очередь. Проще выбрать их галками после анализа папки.',process_codecs:'Кодеки, которые нужно перекодировать. Обычно достаточно h264,mpeg4,vp9,av1,unknown. HEVC/H.265 обрабатывается отдельным правилом.',hevc_action:'Что делать с файлами, которые уже H.265 и не требуют уменьшения: пропустить, перенести, скопировать или сделать hardlink.',max_width:'Максимальная ширина итогового видео. Если исходник меньше, он не увеличивается.',max_height:'Максимальная высота итогового видео. Если исходник меньше, он не увеличивается.',crf:'Главный параметр качества H.265. Меньше число = выше качество и больше размер. Обычно 22-26.',preset:'Скорость кодирования. Medium — нормальный баланс. Slow может жать лучше, но медленнее.',audio_mode:'Что делать со звуком. AAC 160k — самый совместимый вариант для MP4.',audio_bitrate:'Битрейт звука при AAC. Обычно 128k-192k достаточно.',delete_source_after_success:'Опасная настройка: удалять исходник после успешной конвертации. Лучше держать выключенной.',use_failed_path:'Если включено, битые файлы после ошибок будут уходить в карантин.',failed_path:'Папка карантина для файлов, которые не удалось обработать.',use_temp_path:'Обычно не нужно. Если выключено, временный файл создаётся рядом с итоговым и потом переименовывается.',temp_path:'Отдельная временная папка. Нужна редко: например, если хочешь писать временные файлы на другой диск.',filter_min_width:'Фильтр по исходному видео. 0 = не учитывать.',filter_max_width:'Фильтр по исходному видео. 0 = не учитывать.',filter_min_height:'Фильтр по исходному видео. 0 = не учитывать.',filter_max_height:'Фильтр по исходному видео. 0 = не учитывать.',min_duration_seconds:'Минимальная длительность исходника. 0 = не учитывать.',max_duration_seconds:'Максимальная длительность исходника. 0 = не учитывать.',min_size_mb:'Минимальный размер файла. 0 = не учитывать.',max_size_mb:'Максимальный размер файла. 0 = не учитывать.',include_pattern:'Брать только файлы, в пути которых есть этот текст. Пусто = не использовать.',exclude_pattern:'Не брать файлы, в пути которых есть этот текст. Пусто = не использовать.',ffmpeg_threads:'Количество потоков FFmpeg. 0 = автоматически. Можно ограничить, чтобы меньше грузить CPU.',max_retries:'Сколько раз пробовать файл после ошибки. Обычно 1, чтобы не было бесконечной долбёжки.',auto_safe_retry:'Если обычный MP4 падает, попробовать безопасный режим для битых таймкодов/аудио.',safe_remux_to_mp4:'После безопасного MKV попробовать переложить результат обратно в MP4.',allow_mkv_fallback:'Если MP4 не получается, разрешить оставить MKV. Выключи, если Umbrel плохо показывает MKV.',extra_ffmpeg_args:'Дополнительные аргументы FFmpeg. Лучше не трогать, если не понимаешь.'};
const statusRu={pending:'ожидает',waiting_file_copy:'ждём окончания копирования',probing:'читаем параметры',converting:'конвертация',success:'готово',failed:'ошибка',failed_terminal:'ошибка без повтора',moved_to_failed:'в карантине',skipped:'пропущено',skip_ok:'уже готово',moved_already_hevc:'H.265 перенесён',copied_already_hevc:'H.265 скопирован',hardlinked_already_hevc:'H.265 hardlink'};
function st(s){return statusRu[s]||s||''} function esc(s){return String(s==null?'':s).replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]||c))} function jsq(s){return JSON.stringify(String(s==null?'':s))} function fmtPath(p){return esc(p||'')}
async function getJson(u){let r=await fetch(u);return await r.json()} async function postJson(u,d){let r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d||{})});return await r.json()} async function apiPost(u){await postJson(u,{});await refresh()}
function showTab(id){['dashboard','settings','jobs','logs'].forEach(x=>document.getElementById(x).classList.toggle('hidden',x!==id)); if(id==='settings')loadSettings(); if(id==='jobs')loadJobs(); if(id==='logs')loadLogs('app')}
async function refresh(){try{const s=await getJson('/api/stats');document.getElementById('progressText').textContent=s.done+' / '+s.total;document.getElementById('progressPercent').textContent=s.progress_percent+'%';document.getElementById('progressBar').style.width=s.progress_percent+'%';document.getElementById('savedText').textContent=s.saved_h;document.getElementById('savedPercent').textContent=s.saved_percent+'% · '+s.elapsed_h;document.getElementById('volumeText').textContent=s.source_h+' → '+s.output_h;document.getElementById('failedText').textContent=s.failed;document.getElementById('serviceText').textContent='авто: '+(s.auto_convert_enabled?'ВКЛ':'ВЫКЛ')+' · worker: '+(s.worker_enabled?'ВКЛ':'ВЫКЛ');let c=s.current;if(c){document.getElementById('currentBox').innerHTML='<div class="mono">'+fmtPath(c.source_path)+'</div><div>статус: '+esc(st(c.status))+' · '+(c.progress_percent||0)+'% · fps '+esc(c.fps||'')+' · скорость '+esc(c.speed||'')+'</div><div class="mono">результат: '+fmtPath(c.output_path)+'</div>';document.getElementById('currentBar').style.width=(c.progress_percent||0)+'%'}else{document.getElementById('currentBox').textContent='Ожидание';document.getElementById('currentBar').style.width='0%'}}catch(e){console.log(e)}}
const fields={
path:[['input_path','Входящий файл или папка','path-file','span2'],['output_path','Выходная папка','path-dir','span2'],['auto_convert_enabled','Автоконвертация','checkbox',''],['worker_enabled','Фоновый обработчик','checkbox',''],['preserve_input_folder','Сохранять имя входной папки в результате','checkbox',''],['delete_source_after_success','Удалять исходник после успеха','checkbox','']],
simple:[['allowed_extensions','Форматы для обработки','text','span2'],['process_codecs','Кодеки для перекодирования','text','span2'],['hevc_action','Уже H.265','select',''],['container','Контейнер результата','select',''],['max_width','Макс. ширина результата','number',''],['max_height','Макс. высота результата','number',''],['scale_if_too_large','Уменьшать, если больше лимита','checkbox',''],['crf','CRF качества','number',''],['preset','Preset скорости/качества','select',''],['audio_mode','Аудио','select',''],['audio_bitrate','Битрейт аудио','text',''],['allow_mkv_fallback','Разрешить MKV fallback','checkbox','']],
advanced:[['filter_min_width','Фильтр: мин. ширина','number',''],['filter_max_width','Фильтр: макс. ширина','number',''],['filter_min_height','Фильтр: мин. высота','number',''],['filter_max_height','Фильтр: макс. высота','number',''],['min_duration_seconds','Фильтр: мин. длительность, сек','number',''],['max_duration_seconds','Фильтр: макс. длительность, сек','number',''],['min_size_mb','Фильтр: мин. размер, МБ','number',''],['max_size_mb','Фильтр: макс. размер, МБ','number',''],['include_pattern','Имя должно содержать','text','span2'],['exclude_pattern','Имя не должно содержать','text','span2'],['use_temp_path','Отдельная временная папка','checkbox',''],['temp_path','Папка временных файлов','path-dir','span2'],['use_failed_path','Использовать карантин','checkbox',''],['failed_path','Папка карантина','path-dir','span2'],['copy_metadata','Копировать metadata','checkbox',''],['copy_chapters','Копировать chapters','checkbox',''],['faststart','MP4 faststart','checkbox',''],['ffmpeg_threads','Потоки FFmpeg','number',''],['max_retries','Повторов на файл','number',''],['move_failed_after_retries','После ошибок фиксировать как failed','checkbox',''],['auto_safe_retry','Авто safe retry','checkbox',''],['safe_remux_to_mp4','Safe retry вернуть в MP4','checkbox',''],['extra_ffmpeg_args','Доп. аргументы FFmpeg','text','span4']]
};
const selectOptions={hevc_action:[['skip','пропускать'],['move','переносить'],['copy','копировать'],['hardlink','hardlink']],container:[['mp4','mp4'],['mkv','mkv']],preset:['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'].map(x=>[x,x]),audio_mode:[['aac','AAC 160k'],['copy','копировать'],['none','без звука']]};
function fieldList(){return [...fields.path,...fields.simple,...fields.advanced]}
function label(k,txt){return '<label>'+esc(txt)+' <span class="q" data-tip="'+esc(tips[k]||'Подсказка')+'">?</span></label>'}
function inputHtml(k,txt,type,cls){let v=settingsCache[k]??'';let html='<div class="'+(cls||'')+'">'+label(k,txt); if(type==='select'){html+='<select id="set_'+k+'">'+(selectOptions[k]||[['true','true'],['false','false']]).map(o=>'<option value="'+esc(o[0])+'">'+esc(o[1])+'</option>').join('')+'</select>'}else if(type==='checkbox'){html+='<select id="set_'+k+'" onchange="syncDisabled()"><option value="true">включено</option><option value="false">выключено</option></select>'}else if(type==='path-dir'||type==='path-file'){html+='<div class="row" style="flex-wrap:nowrap"><input class="mono" id="set_'+k+'" value="'+esc(v)+'"><button class="btn" type="button" onclick="openBrowser(\''+k+'\',\''+(type==='path-file'?'file':'dir')+'\')">выбрать</button></div>'}else{html+='<input id="set_'+k+'" type="'+(type==='number'?'number':'text')+'" value="'+esc(v)+'">'} return html+'</div>'}
function renderSettings(){document.getElementById('pathForm').innerHTML=fields.path.map(f=>inputHtml(...f)).join('');document.getElementById('simpleForm').innerHTML=fields.simple.map(f=>inputHtml(...f)).join('');document.getElementById('advancedForm').innerHTML=fields.advanced.map(f=>inputHtml(...f)).join('');fieldList().forEach(f=>{let el=document.getElementById('set_'+f[0]); if(el)el.value=String(settingsCache[f[0]]??'')});renderPresets();syncDisabled()}
async function loadSettings(){settingsCache=await getJson('/api/settings');renderSettings()}
function collectSettings(){let obj={};fieldList().forEach(f=>{let el=document.getElementById('set_'+f[0]); if(!el)return;let val=el.value;if(f[2]==='number')val=Number(val);if(f[2]==='checkbox')val=(val==='true');obj[f[0]]=val});return obj}
function syncDisabled(){let temp=document.getElementById('set_use_temp_path')?.value==='true';let fail=document.getElementById('set_use_failed_path')?.value==='true';let tp=document.getElementById('set_temp_path');if(tp)tp.disabled=!temp;let fp=document.getElementById('set_failed_path');if(fp)fp.disabled=!fail}
function renderPresets(){const p=[['balanced','Баланс','H.265 CRF 24, medium, до 4096×2048'],['quality','Качество','CRF 22, slow, больше размер'],['small','Сжать сильнее','CRF 26, medium, меньше размер'],['compat','Совместимый MP4','MP4 + AAC + safe remux'],['nohevc','Не трогать H.265','готовые HEVC пропускать']];document.getElementById('presetButtons').innerHTML=p.map(x=>'<button class="btn preset" onclick="applyPreset(\''+x[0]+'\')"><b>'+x[1]+'</b><span>'+x[2]+'</span></button>').join('')}
function setVal(k,v){let el=document.getElementById('set_'+k); if(el)el.value=String(v)}
function applyPreset(id){if(id==='balanced'){setVal('video_encoder','libx265');setVal('crf',24);setVal('preset','medium');setVal('container','mp4');setVal('audio_mode','aac');setVal('audio_bitrate','160k');setVal('max_width',4096);setVal('max_height',2048);setVal('scale_if_too_large',true);setVal('auto_safe_retry',true);setVal('safe_remux_to_mp4',true);setVal('allow_mkv_fallback',false)} if(id==='quality'){setVal('crf',22);setVal('preset','slow');setVal('audio_bitrate','192k')} if(id==='small'){setVal('crf',26);setVal('preset','medium');setVal('audio_bitrate','128k')} if(id==='compat'){setVal('container','mp4');setVal('audio_mode','aac');setVal('safe_remux_to_mp4',true);setVal('allow_mkv_fallback',false)} if(id==='nohevc'){setVal('hevc_action','skip')} }
function setResolutionPreset(id){let map={"720p":[1280,720],"1080p":[1920,1080],"2k":[2560,1440],"4k":[3840,2160],"4096x2048":[4096,2048]};let m=map[id];if(m){setVal('max_width',m[0]);setVal('max_height',m[1]);setVal('scale_if_too_large',true)}}
async function saveSettings(){let obj=collectSettings();await postJson('/api/settings',obj);await loadSettings();alert('Настройки сохранены')}
function clearPaths(){['input_path','output_path','temp_path','failed_path'].forEach(k=>setVal(k,''))}
async function resetSettings(){if(!confirm('Сбросить настройки к пустым путям и безопасным значениям?'))return;settingsCache=await postJson('/api/settings/reset',{});renderSettings();alert('Настройки сброшены')}
async function clearStats(){if(!confirm('Очистить очередь, историю сканирования и логи приложения? Видео не удаляются.'))return;await postJson('/api/jobs/clear',{});await refresh();alert('Статистика очищена')}
function selectAllExt(on){document.querySelectorAll('.extBox').forEach(x=>x.checked=on);applyExtSelection()}
function applyExtSelection(){let arr=[...document.querySelectorAll('.extBox:checked')].map(x=>x.value);setVal('allowed_extensions',arr.join(','))}
function drawPie(stats){let total=stats.reduce((a,x)=>a+x.count,0);let acc=0;let parts=[];stats.forEach((x,i)=>{let a=acc/total*100;acc+=x.count;let b=acc/total*100;parts.push(colors[i%colors.length]+' '+a+'% '+b+'%')});document.getElementById('pie').style.background=total?'conic-gradient('+parts.join(',')+')':'#102126'}
function renderExtChecks(stats){let allowed=(document.getElementById('set_allowed_extensions')?.value||'').split(',').map(x=>x.trim().toLowerCase()).filter(Boolean);let all=!allowed.length;document.getElementById('extChecks').innerHTML=stats.map((x,i)=>'<label class="check"><input class="extBox" type="checkbox" value="'+esc(x.name)+'" '+(all||allowed.includes(x.name)?'checked':'')+' onchange="applyExtSelection()"><span class="sw" style="background:'+colors[i%colors.length]+'"></span><span>.'+esc(x.name)+' · '+x.count+' шт · '+esc(x.bytes_h)+'</span></label>').join('')}
function renderPreviewResult(d){previewData=d;if(!d||!d.ok){document.getElementById('previewSummary').textContent='Ошибка: '+((d&&d.error)||'неизвестная ошибка')+'\n\nВыбранный путь: '+((d&&d.input_path)||'')+'\nПуть внутри контейнера: '+((d&&d.container_input_path)||'')+'\n\n'+(((d&&d.warnings)||[]).join('\n'));return}drawPie(d.ext_stats||[]);renderExtChecks(d.ext_stats||[]);let text='Сканирование завершено.\n\nВыбранный путь: '+(d.input_path||'')+'\nПуть внутри контейнера: '+(d.container_input_path||'')+'\n\nВсего видеофайлов найдено: '+d.scanned+'\nОбщий объём найденного: '+d.all_source_h+'\nПодходит под текущие фильтры: '+d.matched+'\nОбъём подходящих: '+d.source_h+'\nНе подходит: '+d.skipped+'\n';if(d.warnings?.length)text+='\nПредупреждения:\n'+d.warnings.map(x=>'- '+x).join('\n')+'\n';if(d.ext_stats?.length)text+='\nФорматы:\n'+d.ext_stats.map(x=>'- .'+x.name+': '+x.count+' шт, '+x.bytes_h).join('\n')+'\n';if(d.codec_stats?.length)text+='\nКодеки:\n'+d.codec_stats.map(x=>'- '+x.name+': '+x.count+' шт, '+x.bytes_h).join('\n')+'\n';if(d.resolution_stats?.length)text+='\nРазрешения:\n'+d.resolution_stats.map(x=>'- '+x.name+': '+x.count+' шт, '+x.bytes_h).join('\n')+'\n';if(d.samples?.length)text+='\nПримеры подходящих файлов:\n'+d.samples.slice(0,10).map(x=>'- '+x.size_h+' · .'+x.ext+' · '+x.codec+' · '+x.width+'x'+x.height+' · '+x.path).join('\n');document.getElementById('previewSummary').textContent=text}
async function previewFilters(){let obj=collectSettings();document.getElementById('previewSummary').textContent='Запускаю сканирование...';let start=await postJson('/api/preview/start',obj);if(!start.ok){document.getElementById('previewSummary').textContent='Не удалось запустить сканирование: '+(start.error||'unknown');return}for(let i=0;i<100000;i++){let stt=await getJson('/api/preview/status');let elapsed=stt.elapsed_seconds||0;let text='Статус: '+(stt.state||'')+'\n'+(stt.message||'')+'\n\nПросканировано: '+(stt.scanned||0)+' файлов\nПодходит: '+(stt.matched||0)+'\nНе подходит: '+(stt.skipped||0)+'\nВремя: '+elapsed+' сек.\n\nТекущий файл:\n'+(stt.current_path||'');document.getElementById('previewSummary').textContent=text;if(!stt.running){if(stt.result){renderPreviewResult(stt.result)}else if(stt.error){document.getElementById('previewSummary').textContent='Ошибка сканирования: '+stt.error}break}await new Promise(r=>setTimeout(r,700))}}
async function loadJobs(){let stv=document.getElementById('jobStatus').value;let data=await getJson('/api/jobs?limit=500'+(stv?'&status='+encodeURIComponent(stv):''));document.getElementById('jobsBody').innerHTML=data.jobs.map(j=>'<tr><td><span class="pill">'+esc(st(j.status))+'</span></td><td>'+esc(j.progress_percent||0)+'%</td><td class="path mono">'+fmtPath(j.source_path)+'</td><td class="path mono">'+fmtPath(j.output_path)+'</td><td>'+esc(j.source_size_h)+' → '+esc(j.output_size_h)+'</td><td class="path">'+esc(j.error_message||'')+'</td><td><button class="btn" onclick="retryJob(\''+j.id+'\')">повторить</button> <button class="btn yellow" onclick="moveFailed(\''+j.id+'\')">в failed</button></td></tr>').join('')}
async function retryJob(id){await postJson('/api/jobs/'+id+'/retry',{});loadJobs()} async function moveFailed(id){await postJson('/api/jobs/'+id+'/move-to-failed',{});loadJobs()} async function loadLogs(kind){let d=await getJson('/api/logs?kind='+kind+'&lines=300');document.getElementById('logBox').textContent=d.text||''}
function openBrowser(target,mode){browserTarget=target;browserMode=mode;document.getElementById('browserModal').classList.add('show');browseTo(document.getElementById('set_'+target)?.value||'')}function closeBrowser(){document.getElementById('browserModal').classList.remove('show')}
async function browseTo(path){let d=await getJson('/api/fs?path='+encodeURIComponent(path||''));browserCurrent=d.path||'';document.getElementById('browserPath').value=browserCurrent;let html='';if(d.parent){html+='<div class="fileitem" onclick="browseTo('+esc(jsq(d.parent))+')"><div>⬆️</div><div>..</div><div></div><div></div></div>'}if(d.error){html+='<div class="fileitem"><div>⚠️</div><div>'+esc(d.error)+'</div><div></div><div></div></div>'}(d.entries||[]).forEach(e=>{let icon=e.is_dir?'📁':'🎞️';let click=e.is_dir?'browseTo('+esc(jsq(e.path))+')':'selectFile('+esc(jsq(e.path))+')';html+='<div class="fileitem" onclick="'+click+'"><div>'+icon+'</div><div class="mono">'+esc(e.name)+'</div><div class="muted">'+esc(e.type)+'</div><div class="muted">'+esc(e.size_h||'')+'</div></div>'});document.getElementById('browserList').innerHTML=html||'<div class="fileitem"><div></div><div>Пусто</div><div></div><div></div></div>'}
function selectFile(path){if(browserMode==='file'){document.getElementById('set_'+browserTarget).value=path;closeBrowser()}else{browseTo(path)}} function selectCurrentFolder(){if(browserTarget){document.getElementById('set_'+browserTarget).value=browserCurrent;closeBrowser()}}
setInterval(refresh,2000); refresh();
</script>
</body>
</html>
"""
class Handler(BaseHTTPRequestHandler):
    server_version = "AutoH265/" + APP_VERSION
    def log_message(self, fmt, *args):
        return
    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            data = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/health":
            self.send_json({"ok": True, "version": APP_VERSION})
            return
        if path == "/api/settings":
            self.send_json(load_settings())
            return
        if path == "/api/stats":
            self.send_json(stats_payload())
            return
        if path == "/api/preview/status":
            self.send_json(get_preview_status())
            return
        if path == "/api/fs":
            self.send_json(list_path(qs.get("path", [""])[0]))
            return
        if path == "/api/jobs":
            limit = int(qs.get("limit", [300])[0] or 300)
            status = qs.get("status", [""])[0] or None
            jobs = list_jobs(limit=limit, status=status)
            for j in jobs:
                j["source_size_h"] = fmt_bytes(j.get("source_size_bytes") or 0)
                j["output_size_h"] = fmt_bytes(j.get("output_size_bytes") or 0)
            self.send_json({"jobs": jobs})
            return
        if path == "/api/logs":
            kind = qs.get("kind", ["app"])[0]
            lines = int(qs.get("lines", [200])[0] or 200)
            file = FFMPEG_LOG if kind == "ffmpeg" else APP_LOG
            text = ""
            try:
                with open(file, "r", encoding="utf-8", errors="replace") as f:
                    arr = f.readlines()[-lines:]
                text = "".join(arr)
            except Exception as e:
                text = str(e)
            self.send_json({"text": text})
            return
        self.send_json({"error": "not found"}, 404)
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_json()
        if path == "/api/settings":
            self.send_json(save_settings(body))
            return
        if path == "/api/settings/reset":
            self.send_json(reset_settings_to_defaults())
            return
        if path == "/api/jobs/clear":
            clear_jobs_and_logs()
            self.send_json({"ok": True})
            return
        if path == "/api/preview/start":
            self.send_json(start_preview_analysis(body))
            return
        if path == "/api/preview":
            self.send_json(preview_payload(body))
            return
        if path == "/api/scan":
            n = scan_files()
            self.send_json({"ok": True, "added": n})
            return
        if path == "/api/worker/pause":
            s = load_settings(); s["auto_convert_enabled"] = False; save_settings(s); self.send_json({"ok": True}) ; return
        if path == "/api/worker/resume":
            s = load_settings(); s["auto_convert_enabled"] = True; s["worker_enabled"] = True; save_settings(s); self.send_json({"ok": True}) ; return
        if path == "/api/worker/stop-current":
            with worker_control_lock:
                p = current_process
            if p and p.poll() is None:
                try:
                    p.terminate()
                    self.send_json({"ok": True, "terminated": True})
                    return
                except Exception as e:
                    self.send_json({"ok": False, "error": str(e)}, 500)
                    return
            self.send_json({"ok": True, "terminated": False})
            return
        m = re.match(r"^/api/jobs/([^/]+)/retry$", path)
        if m:
            jid = m.group(1); update_job(jid, status="pending", retry_count=0, error_message="", progress_percent=0); self.send_json({"ok": True}); return
        m = re.match(r"^/api/jobs/([^/]+)/move-to-failed$", path)
        if m:
            jid = m.group(1); job = get_job(jid)
            if job:
                move_to_failed(job, load_settings(), "manual move to failed")
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "error": "job not found"}, 404)
            return
        self.send_json({"error": "not found"}, 404)


def handle_signal(signum, frame):
    shutdown_event.set()
    with worker_control_lock:
        p = current_process
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass


def main():
    init_db()
    if not os.path.exists(SETTINGS_PATH):
        save_settings(DEFAULT_SETTINGS)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    port = int(os.environ.get("APP_PORT", "8080"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log_app(f"server_started port={port}")
    try:
        httpd.serve_forever()
    finally:
        shutdown_event.set()
        httpd.server_close()

if __name__ == "__main__":
    main()
