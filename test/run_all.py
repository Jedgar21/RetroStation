#!/usr/bin/env python3
"""
Run the full RetroStation test suite, tier by tier.

  Tier 1  scheduling logic      (always; no deps)
  Tier 2  transcoding -> HLS     (needs ffmpeg)
  Tier 3  server end-to-end HTTP (needs ffmpeg)
  Tier 4  Plex parsing (mock)    (always); Plex live (if PLEX_URL/PLEX_TOKEN set)

Usage:
  python tests/run_all.py
  PLEX_URL=... PLEX_TOKEN=... python tests/run_all.py   # also runs Plex live
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

TIERS = [
    ("Tier 1 - scheduling logic", "test_scheduling.py", False),
    ("Tier 2 - transcoding/HLS", "test_transcoder.py", True),
    ("Tier 3 - server end-to-end", "test_server_e2e.py", True),
    ("Tier 4 - Plex (mock + live)", "test_plex.py", False),
]


def have_ffmpeg():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


def main():
    ff = have_ffmpeg()
    results = []
    for label, fname, needs_ff in TIERS:
        print(f"\n{'='*60}\n{label}\n{'='*60}")
        if needs_ff and not ff:
            print("  SKIP: ffmpeg/ffprobe not installed")
            results.append((label, "SKIP"))
            continue
        rc = subprocess.run([PY, os.path.join(HERE, fname)]).returncode
        results.append((label, "PASS" if rc == 0 else "FAIL"))

    print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
    for label, status in results:
        mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "−"}[status]
        print(f"  {mark} {status:4}  {label}")
    failed = [l for l, s in results if s == "FAIL"]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
