# Testing RetroStation

A four-tier suite, each isolating a different failure mode. Run everything with:

```bash
python tests/run_all.py
```

Or, with a real Plex server, also exercise the live Plex layer:

```bash
PLEX_URL=http://192.168.1.10:32400 PLEX_TOKEN=yourtoken python tests/run_all.py
```

Each tier is also runnable on its own (and works with or without `pytest`).

## Tier 1 — Scheduling logic  ·  no media, no ffmpeg, no Plex

```bash
python tests/test_scheduling.py
```

Pure-Python tests of the deterministic broadcast model using fake content pools:
same wall-clock time → same program at the same offset, grid slots air the right
show at the right time, episodes advance sequentially across days, sign-off
windows (including ones crossing midnight), 24h coverage, ad-break injection.
This is where most real bugs surface, and it runs in milliseconds.

## Tier 2 — Transcoding → HLS  ·  needs ffmpeg

```bash
python tests/test_transcoder.py
```

Generates throwaway clips in different codecs and confirms ffmpeg produces valid,
playable HLS: the re-encode path (mpeg4 source), the copy/remux path (h264
source), session reuse for the same program, restart when the program changes,
and clean process teardown.

## Tier 3 — Server end-to-end  ·  needs ffmpeg

```bash
python tests/test_server_e2e.py
```

Boots the real Flask server on a test port against synthetic clips and drives the
full pipeline over HTTP: `/api/guide` → `/api/now/<n>` → `/hls/<n>/index.m3u8` →
a real `.ts` segment (verified down to the MPEG-TS sync byte). This is the closest
automated check to "open it in a browser."

### The one thing automation can't check
Browser codec playback. After tier 3 is green, do this once manually:

```bash
python server.py          # then open http://localhost:5005
```

Click a channel and confirm video plays. The page uses hls.js, so any modern
desktop or mobile browser should work; Safari plays HLS natively.

## Tier 4 — Plex  ·  mock always, live opt-in

```bash
python tests/test_plex.py                       # mock layer only
PLEX_URL=... PLEX_TOKEN=... python tests/test_plex.py   # + live layer
```

The **mock** layer feeds canned Plex XML through the parser to verify section
discovery, movie parsing, and show/season/episode metadata extraction — no
network needed. The **live** layer is the part that was never validated against a
real server during development, so run it against yours before relying on Plex:
it connects, lists your libraries, pulls one, and checks items have durations and
stream URLs. Set `PLEX_SECTION="TV Shows"` to target a specific library.

### Getting your Plex token
In the Plex web app, play anything → ⋯ → **Get Info** → **View XML**; the token is
in the URL as `X-Plex-Token=...`.

## Recommended order when something breaks

1. Tier 1 fails → a scheduling/logic regression; fastest to debug.
2. Tier 1 passes but Tier 2 fails → an ffmpeg flag/codec issue, independent of
   your library or the server.
3. Tiers 1–2 pass but Tier 3 fails → a server wiring/HTTP problem.
4. Everything passes but a Plex channel misbehaves → run Tier 4 **live** and check
   library names with `python retrostation.py plex-sections --name <server>`.

## With pytest (optional)

All test files use plain `assert`, so pytest works too:

```bash
pip install pytest
pytest tests/ -v                    # tiers 1, 2, 3, and Plex mock
```

Live Plex functions are named `live_test_*` so pytest ignores them by default;
run them through `tests/test_plex.py` with the env vars set.
