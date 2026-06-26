"""
app/services/external.py — Plex & Jellyfin clients + sync.

Each client lists the server's libraries/categories and imports their items as
MediaItem rows tagged with source + category, so the Media page can build
navigation from the user's own Plex/Jellyfin structure.

Plex     : XML API (/library/sections, /library/sections/{k}/all, allLeaves)
Jellyfin : JSON API (/Library/MediaFolders, /Items?ParentId=...)

Imported MediaItems store:
  source_kind   = plex | jellyfin
  source_id     = ExternalSource.id
  external_key  = ratingKey (Plex) / Id (Jellyfin)
  category      = library name (e.g. "TV Shows", "Music Videos")
  path          = a directly-playable URL for the part/stream (token embedded)

The streamer reads `path` via ffmpeg, so remote Plex/Jellyfin files transcode
just like local ones.
"""

import urllib.parse
import urllib.request
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import List, Optional

from sqlmodel import Session, select

from app.models import (ExternalSource, MediaItem, MediaType, SourceKind)

UTC = timezone.utc


def _get(url: str, headers: dict = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------------- #
# Plex
# --------------------------------------------------------------------------- #
class PlexClient:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.token = token

    def _xml(self, path: str, **params) -> ET.Element:
        params["X-Plex-Token"] = self.token
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        return ET.fromstring(_get(url, {"Accept": "application/xml"}))

    def part_url(self, key: str) -> str:
        sep = "&" if "?" in key else "?"
        return f"{self.base}{key}{sep}X-Plex-Token={self.token}"

    def categories(self) -> List[dict]:
        root = self._xml("/library/sections")
        return [{"key": d.get("key"), "title": d.get("title"),
                 "type": d.get("type")}
                for d in root.findall(".//Directory")]

    def _media_type(self, plex_type: str, title: str) -> MediaType:
        t = (title or "").lower()
        if "commercial" in t or "bumper" in t:
            return MediaType.commercial
        if "music" in t:
            return MediaType.music_video
        if plex_type == "movie":
            return MediaType.movie
        if plex_type == "show":
            return MediaType.show
        return MediaType.other

    def items(self, section_key: str, section_title: str,
              section_type: str) -> List[dict]:
        out = []
        mt = self._media_type(section_type, section_title)
        if section_type == "show":
            shows = self._xml(f"/library/sections/{section_key}/all")
            for show in shows.findall(".//Directory"):
                rk = show.get("ratingKey")
                show_title = show.get("title", "")
                if not rk:
                    continue
                leaves = self._xml(f"/library/metadata/{rk}/allLeaves")
                for v in leaves.findall(".//Video"):
                    d = self._video(v, mt, section_title)
                    if d:
                        d["show_title"] = show_title
                        out.append(d)
        else:
            allv = self._xml(f"/library/sections/{section_key}/all")
            for v in allv.findall(".//Video"):
                d = self._video(v, mt, section_title)
                if d:
                    out.append(d)
        return out

    def _video(self, v, mt, category) -> Optional[dict]:
        part = v.find(".//Part")
        if part is None:
            return None
        key = part.get("key")
        dur = float(v.get("duration") or part.get("duration") or 0) / 1000.0
        if not key or dur <= 0:
            return None
        return {
            "external_key": v.get("ratingKey"),
            "title": v.get("title", "Untitled"),
            "duration": dur,
            "media_type": mt,
            "season": int(v.get("parentIndex")) if v.get("parentIndex") else None,
            "episode": int(v.get("index")) if v.get("index") else None,
            "description": v.get("summary"),
            "category": category,
            "path": self.part_url(key),
        }


# --------------------------------------------------------------------------- #
# Jellyfin
# --------------------------------------------------------------------------- #
class JellyfinClient:
    def __init__(self, base_url: str, token: str, user_id: Optional[str] = None):
        self.base = base_url.rstrip("/")
        self.token = token
        self.user_id = user_id

    def _hdr(self):
        return {"X-Emby-Token": self.token, "Accept": "application/json"}

    def _json(self, path: str, **params) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return json.loads(_get(url, self._hdr()))

    def stream_url(self, item_id: str) -> str:
        # direct static stream of the original file
        return (f"{self.base}/Videos/{item_id}/stream"
                f"?Static=true&api_key={self.token}")

    def categories(self) -> List[dict]:
        data = self._json("/Library/MediaFolders")
        return [{"key": it["Id"], "title": it.get("Name", ""),
                 "type": it.get("CollectionType", "")}
                for it in data.get("Items", [])]

    def _media_type(self, coll_type: str, title: str) -> MediaType:
        t = (title or "").lower()
        ct = (coll_type or "").lower()
        if "commercial" in t or "bumper" in t:
            return MediaType.commercial
        if "music" in ct or "music" in t:
            return MediaType.music_video
        if ct == "movies":
            return MediaType.movie
        if ct == "tvshows":
            return MediaType.show
        return MediaType.other

    def items(self, parent_id: str, title: str, coll_type: str) -> List[dict]:
        mt = self._media_type(coll_type, title)
        params = {
            "ParentId": parent_id, "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode,Video,MusicVideo",
            "Fields": "Path,RunTimeTicks,Overview,IndexNumber,ParentIndexNumber,SeriesName",
        }
        if self.user_id:
            data = self._json(f"/Users/{self.user_id}/Items", **params)
        else:
            data = self._json("/Items", **params)
        out = []
        for it in data.get("Items", []):
            ticks = it.get("RunTimeTicks") or 0
            dur = ticks / 10_000_000.0   # 100ns ticks -> seconds
            if dur <= 0:
                continue
            out.append({
                "external_key": it["Id"],
                "title": it.get("Name", "Untitled"),
                "duration": dur,
                "media_type": mt,
                "season": it.get("ParentIndexNumber"),
                "episode": it.get("IndexNumber"),
                "description": it.get("Overview"),
                "category": title,
                "show_title": it.get("SeriesName"),
                "path": self.stream_url(it["Id"]),
            })
        return out


# --------------------------------------------------------------------------- #
# Sync orchestration
# --------------------------------------------------------------------------- #
def make_client(source: ExternalSource):
    if source.kind == SourceKind.plex:
        return PlexClient(source.base_url, source.token)
    return JellyfinClient(source.base_url, source.token, source.user_id)


def list_categories(source: ExternalSource) -> List[dict]:
    return make_client(source).categories()


def sync_source(session: Session, source: ExternalSource,
                only_categories: Optional[List[str]] = None) -> int:
    """
    Pull all (or selected) categories from the source into MediaItem rows.
    Upserts by (source_id, external_key). Returns count imported/updated.
    """
    client = make_client(source)
    cats = client.categories()
    if only_categories:
        cats = [c for c in cats if c["title"] in only_categories]

    count = 0
    for c in cats:
        try:
            items = client.items(c["key"], c["title"], c["type"])
        except Exception:
            continue
        for d in items:
            existing = session.exec(
                select(MediaItem).where(
                    MediaItem.source_id == source.id,
                    MediaItem.external_key == d["external_key"])).first()
            if existing:
                row = existing
            else:
                row = MediaItem(path=d["path"], source_kind=source.kind,
                                source_id=source.id,
                                external_key=d["external_key"])
            row.path = d["path"]
            row.title = d["title"]
            row.duration = d["duration"]
            row.media_type = d["media_type"]
            row.season = d.get("season")
            row.episode = d.get("episode")
            row.show_title = d.get("show_title")
            row.description = d.get("description")
            row.category = d.get("category")
            session.add(row)
            count += 1
    source.last_synced = datetime.now(UTC)
    session.add(source)
    session.commit()
    return count
