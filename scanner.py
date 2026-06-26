"""
app/services/scanner.py — Scan media folders into MediaItem rows.

Walks a directory, probes durations with ffprobe, infers show/season/episode
from common filename patterns, and upserts MediaItem rows. Folders are typed:
a "commercials" or "bumpers" folder yields commercial items; everything else is
show/movie based on whether SxxExx metadata is detected.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from app.models import MediaItem, MediaType

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".flv"}

# common SxxExx / 1x02 patterns
_SE = re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})")
_XFMT = re.compile(r"\b(\d{1,2})x(\d{1,3})\b")


def probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def parse_episode(filename: str):
    m = _SE.search(filename) or _XFMT.search(filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def infer_show_title(path: str) -> Optional[str]:
    # parent folder name is usually the show title for TV libraries
    parent = Path(path).parent.name
    # strip season folder if present
    if re.match(r"(?i)season\s*\d+", parent):
        parent = Path(path).parent.parent.name
    return parent or None


def scan_folder(session: Session, folder: str,
                media_type: Optional[MediaType] = None) -> int:
    """
    Scan `folder` into MediaItem rows. If media_type is None it's inferred:
    files with SxxExx => show; else movie. A folder named commercials/bumpers
    should be passed media_type=commercial explicitly by the caller.
    Returns the number of items added or updated.
    """
    if not os.path.isdir(folder):
        return 0
    count = 0
    for root, _, files in os.walk(folder):
        for f in sorted(files):
            if Path(f).suffix.lower() not in VIDEO_EXTS:
                continue
            path = os.path.join(root, f)
            season, episode = parse_episode(f)
            if media_type:
                mtype = media_type
            elif season and episode:
                mtype = MediaType.show
            else:
                mtype = MediaType.movie

            existing = session.exec(
                select(MediaItem).where(MediaItem.path == path)).first()
            dur = existing.duration if (existing and existing.duration) else probe_duration(path)
            title = Path(f).stem
            show = infer_show_title(path) if mtype == MediaType.show else None

            if existing:
                existing.duration = dur
                existing.media_type = mtype
                existing.season = season
                existing.episode = episode
                existing.show_title = show
                session.add(existing)
            else:
                session.add(MediaItem(
                    path=path, media_type=mtype, title=title, duration=dur,
                    show_title=show, season=season, episode=episode))
            count += 1
    session.commit()
    return count
