#!/usr/bin/env python3
"""
Shared test fixtures for RetroStation tiers 2 & 3.

Generates throwaway video clips using ffmpeg's built-in synthetic sources, in a
few different codecs/containers so we exercise both the transcode (re-encode)
and remux (copy) code paths. Nothing here touches your real media.
"""

import os
import shutil
import subprocess
import tempfile


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _gen(path, seconds, codec, freq, size="320x240", rate=15):
    """Render one synthetic clip. Returns True on success."""
    if codec == "mpeg4":
        vargs = ["-c:v", "mpeg4"]
    elif codec == "h264":
        vargs = ["-c:v", "libx264", "-preset", "ultrafast"]
    else:
        vargs = ["-c:v", codec]
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate={rate}",
           "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
           *vargs, "-c:a", "aac", "-shortest", path]
    return subprocess.run(cmd).returncode == 0


def make_media_dirs(root=None):
    """
    Create a temp tree with:
      programs/clipA.avi  (mpeg4 -> forces re-encode)
      programs/clipB.mp4  (h264  -> exercises copy/remux path)
      programs/clipC.mp4  (h264, different length)
      ads/ad1.mp4         (h264 short bumper)
    Returns (root, programs_dir, ads_dir). Caller cleans up via shutil.rmtree.
    """
    root = root or tempfile.mkdtemp(prefix="retrostation_test_")
    progs = os.path.join(root, "programs")
    ads = os.path.join(root, "ads")
    os.makedirs(progs, exist_ok=True)
    os.makedirs(ads, exist_ok=True)

    ok = True
    ok &= _gen(os.path.join(progs, "clipA.avi"), 20, "mpeg4", 440)
    ok &= _gen(os.path.join(progs, "clipB.mp4"), 15, "h264", 660)
    ok &= _gen(os.path.join(progs, "clipC.mp4"), 25, "h264", 550)
    ok &= _gen(os.path.join(ads, "ad1.mp4"), 8, "h264", 880)
    if not ok:
        raise RuntimeError("ffmpeg failed to generate fixtures")
    return root, progs, ads


if __name__ == "__main__":
    if not have_ffmpeg():
        print("ffmpeg not found"); raise SystemExit(1)
    r, p, a = make_media_dirs()
    print("fixtures at:", r)
    for d in (p, a):
        for f in sorted(os.listdir(d)):
            print("  ", os.path.join(d, f))
