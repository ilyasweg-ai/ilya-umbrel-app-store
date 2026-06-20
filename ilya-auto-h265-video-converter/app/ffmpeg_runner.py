from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings


@dataclass
class VideoProbe:
    codec: str
    width: int
    height: int
    duration: float
    size_bytes: int


def parse_progress_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or "=" not in line:
        return None
    key, value = line.split("=", 1)
    return key.strip(), value.strip()


def progress_from_out_time(out_time_ms: str, duration: float) -> float:
    if duration <= 0:
        return 0.0
    try:
        seconds = int(out_time_ms) / 1_000_000
    except ValueError:
        return 0.0
    return min(99.9, max(0.0, seconds / duration * 100))


def safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def probe_video(path: Path, ffprobe_bin: str = "ffprobe") -> VideoProbe:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,duration",
        "-show_entries",
        "format=duration,size",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("No video stream found")
    stream = streams[0]
    fmt = payload.get("format") or {}
    duration = safe_float(stream.get("duration"), safe_float(fmt.get("duration"), 0))
    size = int(safe_float(fmt.get("size"), path.stat().st_size))
    return VideoProbe(
        codec=str(stream.get("codec_name") or "unknown").lower(),
        width=int(stream.get("width") or 0),
        height=int(stream.get("height") or 0),
        duration=duration,
        size_bytes=size,
    )


def needs_scaling(probe: VideoProbe, settings: Settings) -> bool:
    return probe.width > settings.max_width or probe.height > settings.max_height


def video_filter(probe: VideoProbe, settings: Settings) -> str:
    if needs_scaling(probe, settings):
        return (
            f"scale='min({settings.max_width},iw)':'min({settings.max_height},ih)'"
            f":force_original_aspect_ratio=decrease:force_divisible_by=2,format={settings.pixel_format}"
        )
    return f"format={settings.pixel_format}"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for index in range(1, 10_000):
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Cannot find unique path for {path}")


def output_path_for(source: Path, settings: Settings) -> Path:
    output_dir = Path(settings.output_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    return unique_path(output_dir / f"{source.stem}.{settings.container}")


def temp_path_for(output_path: Path, settings: Settings) -> Path:
    if settings.use_temp_path:
        temp_dir = Path(settings.temp_path)
        temp_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp_dir = output_path.parent
    return unique_path(temp_dir / f"{output_path.stem}.tmp{output_path.suffix}")


def build_ffmpeg_command(source: Path, temp_output: Path, probe: VideoProbe, settings: Settings) -> list[str]:
    container = settings.container.lower()
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-vf",
        video_filter(probe, settings),
        "-c:v",
        settings.video_encoder,
        "-preset",
        settings.preset,
        "-crf",
        str(settings.crf),
        "-pix_fmt",
        settings.pixel_format,
        "-c:a",
        settings.audio_codec,
        "-b:a",
        settings.audio_bitrate,
    ]
    if settings.ffmpeg_threads:
        cmd.extend(["-threads", str(settings.ffmpeg_threads)])
    if container in {"mp4", "mov", "m4v"}:
        cmd.extend(["-tag:v", "hvc1", "-movflags", "+faststart"])
    cmd.extend(["-progress", "pipe:1", "-nostats", str(temp_output)])
    return cmd


def parse_speed(raw: str) -> str:
    return raw.strip() if raw else ""


def parse_fps(raw: str) -> float | None:
    match = re.match(r"^([0-9.]+)", raw.strip())
    if not match:
        return None
    return safe_float(match.group(1), 0)


def replace_or_move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
    except OSError:
        import shutil

        shutil.move(str(source), str(destination))
