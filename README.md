# FieldStation

A self-hosted retro TV station that turns your media library into continuous,
linear IPTV channels — schedule shows and commercials on a visual timeline, then
tune in from Plex Live TV, Jellyfin, TiViMate, or any IPTV player via a standard
M3U + XMLTV EPG.

## Architecture

```
 ┌─────────────┐   drag&drop    ┌──────────────┐   slots    ┌──────────────┐
 │  Scheduler  │ ─────────────▶ │  FastAPI +   │ ─────────▶ │   SQLite     │
 │  (browser)  │   /api/...     │  SQLModel    │            │ (single file)│
 └─────────────┘                └──────┬───────┘            └──────────────┘
                                       │ playlist_window()
                                       ▼
                                ┌──────────────┐  concat   ┌──────────────┐
                                │  StreamMgr   │ ────────▶ │   ffmpeg     │
                                │ (1 proc/ch)  │ playlist  │ (continuous  │
                                └──────┬───────┘           │  MPEG-TS)    │
                                       │ /stream/2.1.ts    └──────────────┘
                                       ▼
                          Plex / Jellyfin / TiViMate
```

- **Backend:** FastAPI (async), SQLModel/SQLite (single-file DB, WAL mode).
- **Streaming:** one long-lived ffmpeg process per channel, fed a concat
  playlist that a background refiller keeps extended hours ahead of playback, so
  the MPEG-TS never EOFs and IPTV clients never disconnect.
- **Hardware transcoding:** VAAPI / QuickSync (QSV) / NVENC, selected per
  channel via an `FFmpegProfile`.
- **Frontend:** HTML5 drag-and-drop + Tailwind, EPG-style timeline.

### Why the continuous stream is built this way

IPTV clients treat any end-of-stream as a disconnect. FFmpeg's concat demuxer
reads its playlist when the input opens, and appended lines aren't reliably
re-read mid-stream across builds. So instead of appending one file at a time,
the refiller keeps the playlist materialized to cover a long window (default 6h)
of the schedule, far ahead of the play head. The schedule is the source of truth
(rows in `schedule_slot`); the playlist is just its materialization. This is
robust across ffmpeg versions and survives indefinitely.

## Quick start (Docker — recommended)

```bash
# 1. edit docker-compose.yml: set your media path and host render GID
getent group render          # note the GID, put it under group_add
#    change /mnt/media:/media:ro to your library location

docker compose up -d --build

# 2. verify Intel hardware acceleration is visible inside the container
docker compose exec fieldstation vainfo     # should list H264 VAProfiles

# 3. open the scheduler
#    http://<host>:8000
```

## Using it

1. **Scan media** (button, top bar) — point it at `/media/...` folders. Mark a
   folder as TV / Movie / Commercial, or let it auto-detect from `SxxExx` names.
2. **Create a channel** (POST `/api/channels`, or seed one) with a number like
   `2.1` and an FFmpeg profile.
3. **Schedule** — drag a show, movie, or the commercial pool onto an hour. Pick a
   rule: Chronological, Shuffle, or Recurring block, optionally with commercials
   between programs.
4. **Tune in** from your IPTV player:
   - M3U:  `http://<host>:8000/playlist.m3u`
   - EPG:  `http://<host>:8000/epg.xml`
   - Single channel: `http://<host>:8000/stream/2.1.ts`

### Plex Live TV / Jellyfin

Add the M3U as a tuner and the EPG XML as the guide source. Both Plex (via an
HDHomeRun-style M3U tuner) and Jellyfin (Live TV → M3U Tuner + XMLTV) accept
these URLs directly.

## FFmpeg profiles & hardware acceleration

A profile sets resolution, aspect ratio (16:9 or **4:3** for retro pillarboxing),
codecs, bitrate, and the hwaccel mode:

| hwaccel | encoder | hardware |
|---|---|---|
| `none` | libx264 | any (software) |
| `vaapi` | h264_vaapi | Intel/AMD on Linux (QuickSync via VAAPI) |
| `qsv` | h264_qsv | Intel QuickSync native |
| `nvenc` | h264_nvenc | NVIDIA |

For Intel QSV in Docker, the compose file passes `/dev/dri` and adds the host
render group; the image ships `intel-media-va-driver-non-free`. Set the profile's
`hwaccel_device` to `/dev/dri/renderD128`.

## Layout

```
app/
  main.py              FastAPI app + all routes
  models.py            SQLModel schema (MediaItem, Channel, FFmpegProfile, ScheduleSlot)
  database.py          SQLite engine/session (WAL)
  core/
    ffmpeg_cmd.py      profile -> ffmpeg flags (incl. hwaccel paths)
    streamer.py        continuous-stream engine + refiller
  services/
    scheduler.py       rule expansion + playlist windows
    epg.py             XMLTV generator
    scanner.py         media library scan (ffprobe)
  templates/index.html drag-and-drop scheduler UI
Dockerfile
docker-compose.yml
requirements.txt
```

## Notes

- Run a single uvicorn worker: the StreamManager holds ffmpeg processes
  in-process, so multiple workers would each spawn duplicates.
- Times are stored UTC; set `TZ` in compose so the EPG shows your local time.
- The DB lives in `./data` (mounted volume) and persists across restarts.
