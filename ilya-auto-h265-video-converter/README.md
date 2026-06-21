# Auto H265 Converter for Umbrel

MVP web app for automatic FFmpeg conversion.

Default container paths:

- Input: `/media/ssd990_main/porn`
- Output: `/media/ssd990_main/new`
- Failed: `/media/ssd990_main/failed_convert`

On Umbrel host these map to `${UMBREL_ROOT}/external/...`, for example `/home/umbrel/umbrel/external/ssd990_main/porn`.

Features:

- H.264 to H.265/HEVC using FFmpeg/libx265.
- Max resolution limit, default 4096x2048.
- Live current-file progress.
- Queue and statistics.
- Safe retry for broken files.
- Failed quarantine to avoid infinite retry loops.
- UI settings for paths, filters, codec, CRF, preset, audio mode, temp path, retries.
