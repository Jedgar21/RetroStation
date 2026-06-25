#!/usr/bin/env python3
"""
Media source providers for RetroStation.

A source resolves a logical "content pool" into a list of MediaItem objects.
Two providers ship today:

  LocalSource  - walks a directory tree for video files (durations via ffprobe).
  PlexSource   - pulls items from a Plex library section (movies or shows) via
                 the Plex HTTP API. Durations come from Plex metadata (no probe),
                 and playback streams come from Plex too.

Both yield MediaItem objects with a `stream_url()` capability so the transcoder
and player don't need to know where the bytes live.
"""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, quote

import urllib.request
import xml.etree.ElementTree as ET

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".flv"}


def probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


@dataclass
class MediaItem:
    """
    A schedulable piece of content. `source_kind` tells the player how to fetch
    bytes: 'local' -> file path; 'plex' -> a Plex part key + server context.
    """
    title: str
    duration: float                 # seconds
    kind: str = "program"           # program | commercial | bumper
    source_kind: str = "local"      # local | plex
    # local
    path: Optional[str] = None
    # plex
    plex_key: Optional[str] = None          # /library/parts/123/file.mkv key
    plex_rating_key: Optional[str] = None   # for metadata
    show: Optional[str] = None              # series title (for grid scheduling)
    season: Optional[int] = None
    episode: Optional[int] = None
    _server: Optional["PlexServer"] = field(default=None, repr=False)

    def stream_url(self) -> Optional[str]:
        """A URL or path ffmpeg can read directly."""
        if self.source_kind == "local":
            return self.path
        if self.source_kind == "plex" and self._server:
            return self._server.part_url(self.plex_key)
        return None

    def as_dict(self):
        d = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        return d


# --------------------------------------------------------------------------- #
# Local filesystem
# --------------------------------------------------------------------------- #
class LocalSource:
    def __init__(self, directory: str, kind: str = "program"):
        self.directory = directory
        self.kind = kind

    def fetch(self) -> list:
        items = []
        if not self.directory or not os.path.isdir(self.directory):
            return items
        for root, _, files in os.walk(self.directory):
            for f in sorted(files):
                if Path(f).suffix.lower() in VIDEO_EXTS:
                    p = os.path.join(root, f)
                    dur = probe_duration(p)
                    if dur > 0:
                        items.append(MediaItem(
                            title=Path(f).stem, duration=dur,
                            kind=self.kind, source_kind="local", path=p,
                        ))
        return items


# --------------------------------------------------------------------------- #
# Plex
# --------------------------------------------------------------------------- #
class PlexServer:
    """Thin Plex HTTP client. Needs base_url (http://host:32400) and a token."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _get(self, path: str, **params) -> ET.Element:
        params["X-Plex-Token"] = self.token
        url = f"{self.base_url}{path}?{urlencode(params)}"
        req = urllib.request.Request(url, headers={"Accept": "application/xml"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return ET.fromstring(r.read())

    def part_url(self, part_key: str) -> str:
        """Direct stream URL for a media part (original file, no transcode)."""
        sep = "&" if "?" in part_key else "?"
        return f"{self.base_url}{part_key}{sep}X-Plex-Token={self.token}"

    def sections(self) -> list:
        root = self._get("/library/sections")
        return [(d.get("key"), d.get("title"), d.get("type"))
                for d in root.findall(".//Directory")]

    def find_section(self, name: str):
        for key, title, typ in self.sections():
            if title.lower() == name.lower():
                return key, typ
        return None, None


class PlexSource:
    """
    Pull a Plex library section into MediaItems.

    section: the library name as shown in Plex (e.g. "TV Shows", "Movies").
    kind:    program | commercial | bumper (use a dedicated Plex library for ads).
    """

    def __init__(self, server: PlexServer, section: str, kind: str = "program"):
        self.server = server
        self.section = section
        self.kind = kind

    def fetch(self) -> list:
        key, typ = self.server.find_section(self.section)
        if not key:
            raise ValueError(f"Plex section '{self.section}' not found")
        return self._fetch_shows(key) if typ == "show" else self._fetch_movies(key)

    def _part(self, video_el):
        """Extract (part_key, duration_secs) from a <Video> element."""
        part = video_el.find(".//Part")
        if part is None:
            return None, 0.0
        dur_ms = video_el.get("duration") or part.get("duration") or "0"
        return part.get("key"), float(dur_ms) / 1000.0

    def _fetch_movies(self, section_key) -> list:
        root = self.server._get(f"/library/sections/{section_key}/all")
        items = []
        for v in root.findall(".//Video"):
            pkey, dur = self._part(v)
            if pkey and dur > 0:
                items.append(MediaItem(
                    title=v.get("title", "Untitled"), duration=dur,
                    kind=self.kind, source_kind="plex",
                    plex_key=pkey, plex_rating_key=v.get("ratingKey"),
                    _server=self.server,
                ))
        return items

    def _fetch_shows(self, section_key) -> list:
        """Walk every show -> season -> episode in the section."""
        items = []
        shows = self.server._get(f"/library/sections/{section_key}/all")
        for show in shows.findall(".//Directory"):
            rk = show.get("ratingKey")
            show_title = show.get("title", "")
            if not rk:
                continue
            eps = self.server._get(f"/library/metadata/{rk}/allLeaves")
            for v in eps.findall(".//Video"):
                pkey, dur = self._part(v)
                if pkey and dur > 0:
                    items.append(MediaItem(
                        title=f"{show_title} - {v.get('title','')}".strip(" -"),
                        duration=dur, kind=self.kind, source_kind="plex",
                        plex_key=pkey, plex_rating_key=v.get("ratingKey"),
                        show=show_title,
                        season=int(v.get("parentIndex") or 0) or None,
                        episode=int(v.get("index") or 0) or None,
                        _server=self.server,
                    ))
        return items


# --------------------------------------------------------------------------- #
# Factory: build a source from a config dict
# --------------------------------------------------------------------------- #
def build_source(cfg: dict, plex_servers: dict) -> object:
    """
    cfg examples:
      {"type":"local","dir":"/media/toons","kind":"program"}
      {"type":"plex","server":"home","section":"TV Shows","kind":"program"}
    plex_servers: name -> PlexServer
    """
    t = cfg.get("type", "local")
    kind = cfg.get("kind", "program")
    if t == "local":
        return LocalSource(cfg["dir"], kind)
    if t == "plex":
        srv = plex_servers[cfg["server"]]
        return PlexSource(srv, cfg["section"], kind)
    raise ValueError(f"unknown source type: {t}")
