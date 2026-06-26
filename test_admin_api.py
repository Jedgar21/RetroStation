#!/usr/bin/env python3
"""
Tier 5 — Admin API tests. Boots the real server and drives the GUI's backend:
channel CRUD, Plex add/remove, grid slots, transcode settings + encoder
detection. Uses a synthetic local media dir; no Plex or GPU required.

Run:  python tests/test_admin_api.py
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fixtures import have_ffmpeg, make_media_dirs

BASE = "http://127.0.0.1:5098"
ROOT = PROGS = ADS = WORKDIR = SRV = None


def _call(path, method="GET", body=None, timeout=25):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def _setup():
    global ROOT, PROGS, ADS, WORKDIR, SRV
    ROOT, PROGS, ADS = make_media_dirs()
    WORKDIR = os.path.join(ROOT, "run")
    os.makedirs(WORKDIR, exist_ok=True)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=repo)
    boot = ("import server; server.boot(); "
            "server.app.run(host='127.0.0.1', port=5098, threaded=True)")
    SRV = subprocess.Popen([sys.executable, "-c", boot], cwd=WORKDIR, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            _call("/api/admin/state", timeout=2); return
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


try:
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _mod():
        if not have_ffmpeg():
            pytest.skip("ffmpeg/ffprobe not installed")
        _setup(); yield; _teardown()
except ImportError:
    pass


def test_state_has_expected_shape():
    _, st = _call("/api/admin/state")
    assert isinstance(st["channels"], list)
    assert isinstance(st["plex_servers"], list)
    assert "transcode" in st and "encoder" in st["transcode"]


def test_encoder_detection():
    _, e = _call("/api/admin/encoders")
    assert "available" in e and "cpu" in e["available"]
    assert e["available"]["cpu"] is True
    assert e["auto_would_pick"]


def test_create_channel_scans_media():
    status, r = _call("/api/admin/channel", "POST", {
        "number": 3, "name": "ADMIN TV", "mode": "shuffle",
        "program_sources": [{"type": "local", "dir": PROGS, "kind": "program"}],
    })
    assert status == 200 and r["ok"]
    assert r["program_count"] >= 1


def _make_channel(num):
    """Helper: create a fresh channel with the synthetic media pool."""
    return _call("/api/admin/channel", "POST", {
        "number": num, "name": f"CH{num}", "mode": "shuffle",
        "program_sources": [{"type": "local", "dir": PROGS, "kind": "program"}],
    })


def test_channel_appears_in_state():
    _make_channel(10)
    _, st = _call("/api/admin/state")
    assert 10 in [c["number"] for c in st["channels"]]


def test_add_grid_slot_switches_mode():
    _make_channel(11)
    _, r = _call("/api/admin/channel/11/slot", "POST",
                 {"days": "mon,tue,wed", "start": "18:00", "block": 30})
    assert r["ok"]
    _, st = _call("/api/admin/state")
    ch = next(c for c in st["channels"] if c["number"] == 11)
    assert ch["mode"] == "grid"
    assert len(ch["slots"]) == 1


def test_delete_slot():
    _make_channel(12)
    _call("/api/admin/channel/12/slot", "POST", {"days": "fri", "start": "20:00"})
    _, st = _call("/api/admin/state")
    ch = next(c for c in st["channels"] if c["number"] == 12)
    n = len(ch["slots"])
    _, r = _call(f"/api/admin/channel/12/slot/{n-1}", "DELETE")
    assert r["ok"]
    _, st2 = _call("/api/admin/state")
    ch2 = next(c for c in st2["channels"] if c["number"] == 12)
    assert len(ch2["slots"]) == n - 1


def test_rescan_channel():
    _make_channel(13)
    status, r = _call("/api/admin/channel/13/rescan", "POST")
    assert status == 200 and r["ok"]
    assert r["program_count"] >= 1


def test_transcode_update_and_fallback_note():
    # request a GPU encoder that won't exist on the test box
    _, r = _call("/api/admin/transcode", "POST",
                 {"encoder": "nvenc", "video_bitrate": "5000k"})
    assert r["ok"]
    # active encoder is either nvenc (if GPU) or cpu (fallback w/ note)
    assert r["active_encoder"] in ("nvenc", "cpu")
    if r["active_encoder"] == "cpu":
        assert r["note"]
    # and the setting persisted
    _, st = _call("/api/admin/state")
    assert st["transcode"]["video_bitrate"] == "5000k"


def test_plex_add_and_remove():
    _, r = _call("/api/admin/plex", "POST", {
        "name": "testsrv", "base_url": "http://127.0.0.1:32400",
        "token": "FAKE"})
    assert r["ok"]
    _, st = _call("/api/admin/state")
    assert any(p["name"] == "testsrv" for p in st["plex_servers"])
    _, r2 = _call("/api/admin/plex/testsrv", "DELETE")
    assert r2["ok"]
    _, st2 = _call("/api/admin/state")
    assert not any(p["name"] == "testsrv" for p in st2["plex_servers"])


def test_delete_channel():
    _call("/api/admin/channel", "POST", {
        "number": 14, "name": "CH14", "mode": "shuffle",
        "program_sources": [{"type": "local", "dir": PROGS, "kind": "program"}]})
    _, r = _call("/api/admin/channel/14", "DELETE")
    assert r["ok"]
    _, st = _call("/api/admin/state")
    assert 14 not in [c["number"] for c in st["channels"]]


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
