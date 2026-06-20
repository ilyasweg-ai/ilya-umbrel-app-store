from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    input_path: str = field(default_factory=lambda: os.getenv("DEFAULT_INPUT_PATH", "/media/input"))
    output_path: str = field(default_factory=lambda: os.getenv("DEFAULT_OUTPUT_PATH", "/media/output"))
    failed_path: str = field(default_factory=lambda: os.getenv("DEFAULT_FAILED_PATH", "/media/failed"))
    temp_path: str = field(default_factory=lambda: os.getenv("DEFAULT_TEMP_PATH", "/media/temp"))
    use_temp_path: bool = field(default_factory=lambda: _env_bool("USE_TEMP_PATH", False))
    worker_enabled: bool = True
    auto_convert_enabled: bool = True
    recursive_scan: bool = True
    scan_interval_seconds: int = 5
    stable_file_seconds: int = 1
    max_retries: int = 1
    allowed_extensions: list[str] = field(
        default_factory=lambda: [".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm"]
    )
    video_encoder: str = "libx265"
    container: str = "mp4"
    crf: int = 24
    preset: str = "medium"
    max_width: int = 4096
    max_height: int = 2048
    pixel_format: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "160k"
    ffmpeg_threads: int = field(default_factory=lambda: _env_int("FFMPEG_THREADS", 0))
    already_hevc_action: str = "skip"

    def normalized(self) -> "Settings":
        self.input_path = str(Path(self.input_path))
        self.output_path = str(Path(self.output_path))
        self.failed_path = str(Path(self.failed_path))
        self.temp_path = str(Path(self.temp_path))
        self.scan_interval_seconds = max(1, int(self.scan_interval_seconds))
        self.stable_file_seconds = max(0, int(self.stable_file_seconds))
        self.max_retries = max(1, int(self.max_retries))
        self.crf = min(32, max(18, int(self.crf)))
        self.max_width = max(2, int(self.max_width))
        self.max_height = max(2, int(self.max_height))
        self.ffmpeg_threads = max(0, int(self.ffmpeg_threads))
        self.allowed_extensions = [
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in self.allowed_extensions
            if str(ext).strip()
        ]
        if not self.allowed_extensions:
            self.allowed_extensions = [".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm"]
        if self.preset not in {
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
        }:
            self.preset = "medium"
        if self.already_hevc_action not in {"skip", "copy"}:
            self.already_hevc_action = "skip"
        return self


class SettingsStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / "config.json"
        self._lock = threading.RLock()
        self._settings = self._load()

    def _load(self) -> Settings:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        settings = Settings()
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for key, value in raw.items():
                if hasattr(settings, key):
                    setattr(settings, key, value)
        settings.normalized()
        self._ensure_paths(settings)
        self._write(settings)
        return settings

    def _write(self, settings: Settings) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(asdict(settings), fh, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def _ensure_paths(self, settings: Settings) -> None:
        for value in {
            settings.input_path,
            settings.output_path,
            settings.failed_path,
            settings.temp_path,
        }:
            Path(value).mkdir(parents=True, exist_ok=True)

    def get(self) -> Settings:
        with self._lock:
            return Settings(**asdict(self._settings)).normalized()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self.get())

    def update(self, patch: dict[str, Any]) -> Settings:
        with self._lock:
            data = asdict(self._settings)
            for key, value in patch.items():
                if key in data:
                    data[key] = value
            settings = Settings(**data).normalized()
            self._ensure_paths(settings)
            self._write(settings)
            self._settings = settings
            return self.get()

