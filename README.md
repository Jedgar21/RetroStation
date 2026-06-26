# RetroStation

A linear TV broadcast simulator in the spirit of **ErsatzTV** and **FieldStation42**.
Point it at media — **local folders and/or Plex libraries** — and each becomes a
"channel" broadcasting on a continuous 24h schedule. Tune in and you join whatever
is **currently airing, in progress**, transcoded live so any codec plays in any browser.

## What's new in v2

- **Live HLS transcoding** (ffmpeg): any source codec → H.264/AAC HLS, seeked to
  the broadcast offset. Already-compatible files are remuxed (copied) to save CPU.
- **Plex as a media source** alongside local files — pull whole libraries (Movies
  or TV Shows) by name; episodes carry season/episode metadata for grid scheduling.
- **Weekly schedule grid**: define recurring slots ("Mon–Fri 18:00 → *The News*,
  30m") and the engine airs sequential episodes in each slot, deterministically,
  padding gaps with filler. Or keep the original **shuffle** mode.

## Architecture

| File | Role |
|---|---|
| `sources.py` | Pluggable media providers: `LocalSource`, `PlexSource` (+ `PlexServer` client) |
| `station.py` | Scheduling engine — `Channel` (shuffle + grid modes), `Slot`, `Station` |
| `transcoder.py` | Per-channel ffmpeg → live HLS sessions, restart-on-program-change |
| `server.py` | Flask UI + `/api/guide`, `/api/now/<n>`, `/hls/<n>/...` |
| `retrostation.py` | CLI to manage Plex servers, channels, and grid slots |

Schedules are deterministic: the same wall-clock time always maps to the same
program at the same offset, so channels "broadcast" whether or not anyone watches.

## Requirements

- Python 3.9+, `ffmpeg`/`ffprobe`
- `pip install flask`
- (optional) a Plex Media Server + token for Plex sources

## Setup

```bash
pip install flask

# --- optional: connect Plex ---
python retrostation.py plex-add --name home \
    --url http://192.168.1.10:32400 --token YOURPLEXTOKEN
python retrostation.py plex-sections --name home     # see exact library names

# --- a local shuffle channel with ad breaks every 20 min ---
python retrostation.py channel-add --number 2 --name "TOON TV" --mode shuffle \
    --local ~/media/cartoons --ads-local ~/media/commercials --break-every 1200

# --- a shuffle channel from a Plex movie library ---
python retrostation.py channel-add --number 4 --name "MOVIE NIGHT" --mode shuffle \
    --plex home:Movies

# --- a weekly GRID channel from Plex TV Shows ---
python retrostation.py channel-add --number 5 --name "PRIMETIME" --mode grid \
    --plex home:"TV Shows"
python retrostation.py slot-add --channel 5 --days mon,tue,wed,thu,fri \
    --start 18:00 --show "The News" --block 30
python retrostation.py slot-add --channel 5 --days mon,tue,wed,thu,fri \
    --start 18:30 --show "Cheers" --block 30

# --- run it ---
python retrostation.py guide      # what's on right now
python server.py                  # web UI at http://localhost:5005
python retrostation.py watch 5    # ffplay, joins in progress
```

## Finding your Plex token

In the Plex web app, play any item → ⋯ → **Get Info** → **View XML**; the URL
contains `X-Plex-Token=...`. (Or check `Preferences.xml` on the server.)

## Configuring from the browser

Everything can be set up from the web UI — no CLI or JSON editing required.
Start the server (`python server.py`) and click **CONFIGURE** (top-right of the
TV guide), or go straight to `http://localhost:5005/admin`. Three tabs:

- **Channels** — create, edit, delete channels; add local-folder or Plex-library
  sources; build weekly grid slots; set ad breaks and sign-off windows. "Rescan"
  re-indexes a channel's sources after you add media.
- **Plex** — connect/disconnect Plex servers and list their libraries (so you can
  copy exact names into a channel's sources).
- **Transcoding** — live hardware detection (shows which encoders actually work
  on this machine), plus the encoder picker, device, bitrate, preset, and segment
  length. Applying changes rebuilds the encoder immediately.

The CLI (`retrostation.py`) still does everything the GUI does, for headless or
scripted setups. Both read and write the same `station.json`.

## Hardware-accelerated transcoding

By default the transcoder uses `auto`, which probes for a working hardware H.264
encoder and falls back to CPU (`libx264`) if none is usable. Hardware encoding
dramatically lowers CPU load — useful for a small LXC/VM or several simultaneous
viewers.

See what your machine actually supports:

```bash
python retrostation.py encoders
```

This lists the encoders compiled into ffmpeg *and* runtime-tests each one (a
compiled-in encoder still fails without the device/drivers/passthrough), then
shows what `auto` would pick.

Choose an encoder:

```bash
# NVIDIA
python retrostation.py transcode-set --encoder nvenc --bitrate 6000k

# Intel Quick Sync
python retrostation.py transcode-set --encoder qsv

# VAAPI (Intel/AMD on Linux) — point at your render node
python retrostation.py transcode-set --encoder vaapi --device /dev/dri/renderD128

# Apple Silicon / macOS
python retrostation.py transcode-set --encoder videotoolbox

# force software (or let auto handle it)
python retrostation.py transcode-set --encoder cpu
```

| Encoder | ffmpeg codec | Hardware |
|---|---|---|
| `cpu` | libx264 | any (software) |
| `nvenc` | h264_nvenc | NVIDIA GPUs |
| `qsv` | h264_qsv | Intel Quick Sync (iGPU) |
| `vaapi` | h264_vaapi | Intel/AMD on Linux |
| `videotoolbox` | h264_videotoolbox | Apple Silicon/macOS |

Other knobs: `--bitrate` (e.g. `3500k`, `6000k`), `--preset` (backend-specific),
`--hls-time` (segment length), `--no-copy` (always re-encode instead of remuxing
already-compatible files), `--no-verify` (skip the startup probe). Settings are
saved to the `transcode` block of `station.json`; **restart the server** for them
to take effect.

If a requested encoder can't run, the server logs a notice and falls back to CPU
rather than taking channels off the air — so a misconfiguration never blocks
playback.

### Passing a GPU into a container

Hardware encoding needs the device available inside the container:

- **Docker (NVIDIA):** install the NVIDIA Container Toolkit, run with `--gpus all`.
- **Docker (VAAPI/QSV):** pass the render node with `--device /dev/dri`.
- **Proxmox LXC (VAAPI/QSV):** bind-mount `/dev/dri` into the container and use
  `--encoder vaapi --device /dev/dri/renderD128`.

Confirm it worked by running `python retrostation.py encoders` *inside* the
container — the encoder should report `WORKS`.

## Source mixing

A channel can pull from multiple sources at once — repeat `--local` and `--plex`.
Commercials/bumpers come from a separate pool (`--ads-local` / `--ads-plex`); put
your bumpers in a dedicated Plex library or folder and they'll be injected between
programs per `--break-every`.

## Scheduling modes

**shuffle** — deterministic per-day shuffle of the pool, looped to fill 24h, with
optional ad breaks. Good for cartoon/music-video/movie channels.

**grid** — fixed weekly slots. Each slot draws sequential episodes of its show
(advancing day to day), and the engine fills the gaps with filler + ads. Edit
`station.json` for advanced knobs (`commercials_per_break`, `sign_off_start`/`_end`).

## Notes & limits

- One ffmpeg process per *actively watched* channel; idle channels cost nothing.
- HLS is a short sliding live window, so disk/memory stay bounded.
- Plex streams are pulled as direct file parts and transcoded locally by
  RetroStation (Plex isn't asked to transcode).
- `-ss` uses fast input seek (keyframe-accurate), so the join point can be off by
  up to one GOP — imperceptible for TV-style viewing.
