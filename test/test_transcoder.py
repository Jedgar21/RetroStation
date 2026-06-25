#!/usr/bin/env python3
"""
Tier 2 — Transcoding tests. Requires ffmpeg; generates synthetic clips.

Verifies that the live HLS transcoder actually produces playable segments,
seeked to the broadcast offset, for both the re-encode path (mpeg4 source) and
the copy/remux path (h264 source), and that switching the airing item restarts
the session cleanly.

Run:  python tests/test_transcoder.py
"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fixtures import have_ffmpeg, make_media_dirs
from transcoder import Transcoder

ROOT = PROGS = ADS = None
TRANS = None


def _setup():
    global ROOT, PROGS, ADS, TRANS
    ROOT, PROGS, ADS = make_media_dirs()
    TRANS = Transcoder(hls_time=2, window=4)


def _teardown():
    if TRANS:
        TRANS.cleanup()
    if ROOT:
        shutil.rmtree(ROOT, ignore_errors=True)


# pytest entry point: build fixtures once for the module, tear down after.
try:
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _module_fixtures():
        if not have_ffmpeg():
            pytest.skip("ffmpeg/ffprobe not installed")
        _setup()
        yield
        _teardown()
except ImportError:
    pass


def _segs(channel):
    d = os.path.join(TRANS.root, f"ch{channel}")
    return [f for f in os.listdir(d) if f.endswith(".ts")] if os.path.isdir(d) else []


def test_reencode_path_produces_segments():
    """mpeg4 AVI must be re-encoded to H.264 HLS and produce segments."""
    TRANS.start(channel=2, src=os.path.join(PROGS, "clipA.avi"),
                seek=5.0, item_title="clipA")
    assert TRANS.playlist_ready(2, timeout=25), "no playlist for re-encode path"
    assert len(_segs(2)) >= 1, "no segments produced"


def test_copy_path_produces_segments():
    """h264 MP4 should remux (copy) and produce segments quickly."""
    TRANS.start(channel=3, src=os.path.join(PROGS, "clipB.mp4"),
                seek=3.0, item_title="clipB")
    assert TRANS.playlist_ready(3, timeout=25), "no playlist for copy path"
    assert len(_segs(3)) >= 1


def test_playlist_is_valid_hls():
    TRANS.start(channel=4, src=os.path.join(PROGS, "clipC.mp4"),
                seek=2.0, item_title="clipC")
    assert TRANS.playlist_ready(4, timeout=25)
    pl = os.path.join(TRANS.root, "ch4", "index.m3u8")
    text = open(pl).read()
    assert text.startswith("#EXTM3U"), "not an HLS playlist"
    assert "#EXTINF" in text, "no segment entries"
    assert ".ts" in text


def test_ensure_does_not_restart_same_item():
    """Calling ensure() repeatedly for the same program reuses the session."""
    TRANS.start(channel=5, src=os.path.join(PROGS, "clipC.mp4"),
                seek=1.0, item_title="clipC")
    assert TRANS.playlist_ready(5, timeout=25)
    s1 = TRANS.sessions[5]
    pid1 = s1.proc.pid
    TRANS.ensure(5, os.path.join(PROGS, "clipC.mp4"), 4.0, "clipC")
    assert TRANS.sessions[5].proc.pid == pid1, "restarted despite same item"


def test_ensure_restarts_on_item_change():
    """When the airing program changes, the session must restart."""
    TRANS.start(channel=6, src=os.path.join(PROGS, "clipB.mp4"),
                seek=1.0, item_title="clipB")
    assert TRANS.playlist_ready(6, timeout=25)
    pid1 = TRANS.sessions[6].proc.pid
    TRANS.ensure(6, os.path.join(PROGS, "clipC.mp4"), 0.0, "clipC")
    assert TRANS.sessions[6].proc.pid != pid1, "did not restart on item change"


def test_stop_kills_process():
    TRANS.start(channel=7, src=os.path.join(PROGS, "clipB.mp4"),
                seek=0.0, item_title="clipB")
    assert TRANS.playlist_ready(7, timeout=25)
    proc = TRANS.sessions[7].proc
    TRANS.stop(7)
    time.sleep(0.5)
    assert proc.poll() is not None, "ffmpeg still running after stop"
    assert 7 not in TRANS.sessions


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
