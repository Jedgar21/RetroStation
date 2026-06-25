#!/usr/bin/env python3
"""
Tier 3 — End-to-end HTTP test. Boots the real Flask server against synthetic
clips and exercises the full pipeline over HTTP: guide API -> now API ->
HLS playlist -> HLS segment bytes.

This is the closest automated check to "open it in a browser" (it can't verify
browser codec playback, but it proves the server serves valid, playable HLS).

Run:  python tests/test_server_e2e.py
"""

import os
import shutil
import subprocess
import sys
import time
import json
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fixtures import have_ffmpeg, make_media_dirs

BASE = "http://127.0.0.1:5099"
ROOT = PROGS = ADS = None
SRV = None
WORKDIR = None


def _get(path, binary=False, timeout=20):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        data = r.read()
        return (r.status, data if binary else data.decode())


def _setup():
    """Create fixtures, write a station.json, boot the server on port 5099."""
    global ROOT, PROGS, ADS, SRV, WORKDIR
    ROOT, PROGS, ADS = make_media_dirs()

    WORKDIR = os.path.join(ROOT, "run")
    os.makedirs(WORKDIR, exist_ok=True)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # build a station config pointing at the synthetic clips
    cfg = {
        "plex_servers": {},
        "channels": [{
            "number": 2, "name": "E2E TV", "mode": "shuffle",
            "program_sources": [{"type": "local", "dir": PROGS, "kind": "program"}],
            "commercial_sources": [{"type": "local", "dir": ADS, "kind": "commercial"}],
            "shuffle": True, "commercial_break_every": 0.0,
            "commercials_per_break": 2, "slots": [],
            "sign_off_start": None, "sign_off_end": None, "seed": 7,
        }],
    }
    with open(os.path.join(WORKDIR, "station.json"), "w") as f:
        json.dump(cfg, f)

    # run server.py from WORKDIR (so it loads our station.json) on port 5099
    env = dict(os.environ, PYTHONPATH=repo)
    boot = (
        "import sys; sys.argv=['server']; "
        "import server; server.boot(); "
        "server.app.run(host='127.0.0.1', port=5099, threaded=True)"
    )
    SRV = subprocess.Popen([sys.executable, "-c", boot],
                           cwd=WORKDIR, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # wait for it to come up
    for _ in range(40):
        try:
            _get("/api/guide", timeout=2)
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("server did not start")


def _teardown():
    if SRV:
        SRV.terminate()
        try:
            SRV.wait(timeout=5)
        except Exception:
            SRV.kill()
    if ROOT:
        shutil.rmtree(ROOT, ignore_errors=True)


# pytest entry point: boot the server once for the module, tear down after.
try:
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _module_server():
        if not have_ffmpeg():
            pytest.skip("ffmpeg/ffprobe not installed")
        _setup()
        yield
        _teardown()
except ImportError:
    pass


def test_guide_endpoint():
    status, body = _get("/api/guide")
    assert status == 200
    rows = json.loads(body)
    assert len(rows) == 1
    assert rows[0]["channel"] == 2
    assert rows[0]["status"] == "on_air"
    assert rows[0]["title"]


def test_now_endpoint():
    status, body = _get("/api/now/2")
    assert status == 200
    np = json.loads(body)
    assert np["status"] == "on_air"
    assert 0 <= np["seek"] <= np["duration"]
    assert np["stream_url"]


def test_now_endpoint_404_for_unknown_channel():
    try:
        _get("/api/now/999")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_hls_playlist_served():
    status, body = _get("/hls/2/index.m3u8")
    assert status == 200
    assert body.startswith("#EXTM3U"), "not an HLS playlist"
    assert ".ts" in body, "playlist has no segments"


def test_hls_segment_bytes():
    # fetch the playlist, pull the first segment name, download it
    _, pl = _get("/hls/2/index.m3u8")
    seg = next((ln.strip() for ln in pl.splitlines()
                if ln.strip().endswith(".ts")), None)
    assert seg, "no segment in playlist"
    status, data = _get(f"/hls/2/{seg}", binary=True)
    assert status == 200
    assert len(data) > 1000, f"segment too small: {len(data)} bytes"
    # MPEG-TS packets start with sync byte 0x47
    assert data[0] == 0x47, "not a valid MPEG-TS segment"


def test_full_surf_sequence():
    """Simulate a viewer: load guide, tune in, get playlist + a segment."""
    _, guide = _get("/api/guide")
    ch = json.loads(guide)[0]["channel"]
    _, np = _get(f"/api/now/{ch}")
    assert json.loads(np)["status"] == "on_air"
    _, pl = _get(f"/hls/{ch}/index.m3u8")
    assert "#EXTM3U" in pl


if __name__ == "__main__":
    if not have_ffmpeg():
        print("SKIP: ffmpeg/ffprobe not installed"); sys.exit(0)
    _setup()
    try:
        fns = [v for k, v in sorted(globals().items())
               if k.startswith("test_") and callable(v)]
        passed = 0
        for fn in fns:
            try:
                fn(); print(f"  PASS  {fn.__name__}"); passed += 1
            except AssertionError as e:
                print(f"  FAIL  {fn.__name__}: {e}")
            except Exception as e:
                print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
        print(f"\n{passed}/{len(fns)} passed")
        sys.exit(0 if passed == len(fns) else 1)
    finally:
        _teardown()
