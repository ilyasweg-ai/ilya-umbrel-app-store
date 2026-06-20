# Auto H265 Video Converter MVP

Minimal Docker web app for Umbrel OS. It watches one media folder, queues video
files, converts them one at a time to H.265/HEVC with FFmpeg, and shows progress
in a simple web UI.

Umbrel community app id: `ilya-auto-h265-video-converter`.

This MVP intentionally avoids extra UI polish. The goal is one working path:
put one file into the input folder, let the worker convert it, and get an HEVC
MP4 in the output folder.

## What Works Now

- Prebuilt Docker image with Python, FastAPI, FFmpeg, FFprobe, and dependencies
- Small Umbrel `docker-compose.yml` that only pulls the GHCR image
- GitHub Actions workflow for publishing the image to GHCR
- SQLite history in `/data/app.db`
- settings persisted in `/data/config.json`
- logs in `/data/logs/app.log`
- one background worker, one job at a time
- autoscan of the input folder
- stable-file wait before queueing
- H.264 and other non-HEVC video to H.265/HEVC through `libx265`
- already-HEVC files are skipped by default
- max resolution filter: `4096x2048`, no upscale
- CRF and preset settings in the UI
- live progress by polling `/api/progress`
- failed files moved to quarantine after the retry limit
- source files are kept after successful conversion
- Umbrel metadata files: `umbrel-app.yml`, `exports.sh`, `icon.svg`

## Umbrel Host Run

On the Umbrel machine, clone this repo and create the folders you want to use:

```bash
mkdir -p /home/umbrel/umbrel/external/ssd990_main/porn
mkdir -p /home/umbrel/umbrel/external/ssd990_main/new
mkdir -p /home/umbrel/umbrel/external/ssd990_main/failed_convert
mkdir -p /home/umbrel/umbrel/external/ssd990_main/temp_convert
```

Copy the example env file:

```bash
cp .env.example .env
```

Default `.env.example` values expect the Umbrel external root to be mounted into
the container as `/media`:

```env
MEDIA_PATH=/home/umbrel/umbrel/external/ssd990_main
DEFAULT_INPUT_PATH=/media/ssd990_main/porn
DEFAULT_OUTPUT_PATH=/media/ssd990_main/new
DEFAULT_FAILED_PATH=/media/ssd990_main/failed_convert
DEFAULT_TEMP_PATH=/media/ssd990_main/temp_convert
```

Start the app:

```bash
docker compose up -d --build
```

Open:

```text
http://<umbrel-ip>:8080
```

## End-to-End Smoke Test

This test creates a small H.264 file inside the container, scans it, and lets the
worker convert it.

```bash
docker compose exec server ffmpeg -y \
  -f lavfi -i testsrc=size=640x360:rate=24 \
  -f lavfi -i sine=frequency=1000:sample_rate=48000 \
  -t 3 \
  -c:v libx264 -pix_fmt yuv420p \
  -c:a aac \
  /media/ssd990_main/porn/smoke.mp4

curl -X POST http://127.0.0.1:8080/api/scan
curl http://127.0.0.1:8080/api/jobs
```

After a few seconds, the job should become `success` and the result should be:

```text
/home/umbrel/umbrel/external/ssd990_main/new/smoke.mp4
```

Check the output codec:

```bash
docker compose exec server ffprobe -v error \
  -select_streams v:0 \
  -show_entries stream=codec_name,width,height \
  -of default=noprint_wrappers=1 \
  /media/ssd990_main/new/smoke.mp4
```

Expected codec:

```text
codec_name=hevc
```

## Umbrel App Store Notes

The repo includes the minimum app package files:

- `umbrel-app.yml`
- `docker-compose.yml`
- `exports.sh`
- `icon.svg`
- `README.md`

In the `ilya` community app store, this folder must be named:

```text
ilya-auto-h265-video-converter
```

The folder name and `id` in `umbrel-app.yml` must match.

Before installing from Umbrel, the Docker image must exist in GHCR and be public.
This repository includes a workflow at:

```text
.github/workflows/build-ilya-auto-h265-video-converter.yml
```

It builds from:

```text
ilya-auto-h265-video-converter/Dockerfile
```

and publishes:

```text
ghcr.io/ilyasweg-ai/ilya-auto-h265-video-converter:<version>
ghcr.io/ilyasweg-ai/ilya-auto-h265-video-converter:latest
```

For version `0.1.6`, Umbrel pulls:

```text
ghcr.io/ilyasweg-ai/ilya-auto-h265-video-converter:0.1.6
```

## API

- `GET /api/health`
- `GET /api/version`
- `GET /api/settings`
- `PUT /api/settings`
- `POST /api/scan`
- `GET /api/scan/status`
- `GET /api/jobs`
- `GET /api/jobs/{id}`
- `POST /api/jobs/{id}/retry`
- `POST /api/jobs/{id}/skip`
- `POST /api/jobs/{id}/move-to-failed`
- `GET /api/progress`
- `GET /api/stats`
- `POST /api/worker/start`
- `POST /api/worker/stop`
- `POST /api/worker/pause`
- `POST /api/worker/resume`
- `GET /api/logs`

## Defaults

- input path: `/media/ssd990_main/porn`
- output path: `/media/ssd990_main/new`
- failed path: `/media/ssd990_main/failed_convert`
- temp path: `/media/ssd990_main/temp_convert`
- video encoder: `libx265`
- container: `mp4`
- CRF: `24`
- preset: `medium`
- max resolution: `4096x2048`
- pixel format: `yuv420p`
- audio: `aac 160k`
- already HEVC action: `skip`
- max retries: `1`
- source cleanup after success: disabled

## Development Checks

Unit tests:

```bash
python -m unittest discover -s tests
```

Syntax check:

```bash
python -m py_compile app/*.py
```
