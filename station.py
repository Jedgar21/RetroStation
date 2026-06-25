#!/usr/bin/env python3
"""
RetroStation scheduling engine (v2).

Channels support two scheduling modes:

  "shuffle" - loop the program pool in a deterministic per-day order, optionally
              injecting commercial breaks, filling 24h.

  "grid"    - a weekly TV-guide grid. Define recurring slots like
              "Mon-Fri 18:00 -> Show 'The News', 30m" and the engine places the
              right (sequential, deterministic) episode in each slot, padding
              the rest of the day with filler from the pool.

Both modes answer: now_playing(), upcoming(), day_schedule().
Content comes from pluggable sources (local dirs and/or Plex libraries).
"""

import hashlib
import json
import random
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Optional

from sources import MediaItem, PlexServer, build_source

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class Slot:
    """A recurring weekly programming slot."""
    days: list
    start: str                       # "HH:MM"
    show: Optional[str] = None       # series title; None = draw from whole pool
    block: Optional[int] = None      # minutes reserved; None = episode length

    def matches_day(self, dt: datetime) -> bool:
        return DAYS[dt.weekday()] in [d.lower() for d in self.days]

    def start_seconds(self) -> float:
        t = dtime.fromisoformat(self.start)
        return t.hour * 3600 + t.minute * 60 + t.second


@dataclass
class Channel:
    number: int
    name: str
    mode: str = "shuffle"            # shuffle | grid

    program_sources: list = field(default_factory=list)     # list of cfg dicts
    commercial_sources: list = field(default_factory=list)

    shuffle: bool = True
    commercial_break_every: float = 0.0
    commercials_per_break: int = 2

    slots: list = field(default_factory=list)               # list of Slot dicts

    sign_off_start: Optional[str] = None
    sign_off_end: Optional[str] = None
    seed: int = 0

    programs: list = field(default_factory=list, repr=False)
    commercials: list = field(default_factory=list, repr=False)

    def scan(self, plex_servers: dict):
        self.programs, self.commercials = [], []
        for cfg in self.program_sources:
            self.programs += build_source(cfg, plex_servers).fetch()
        for cfg in self.commercial_sources:
            self.commercials += build_source(cfg, plex_servers).fetch()
        if not self.seed:
            self.seed = int(hashlib.md5(self.name.encode()).hexdigest(), 16) % (2**31)

    def _episodes_of(self, show: Optional[str]) -> list:
        pool = list(self.programs) if show is None else \
            [p for p in self.programs if (p.show or "") == show]
        pool.sort(key=lambda m: (m.season or 0, m.episode or 0, m.title))
        return pool

    def _pick_sequential(self, show: Optional[str], index: int):
        eps = self._episodes_of(show)
        return eps[index % len(eps)] if eps else None

    def _ad_break(self, rng, max_secs: float) -> list:
        out, used = [], 0.0
        if not self.commercials:
            return out
        for _ in range(self.commercials_per_break):
            ad = rng.choice(self.commercials)
            if used + ad.duration > max_secs and out:
                break
            out.append(ad); used += ad.duration
        return out

    def _shuffle_day(self, day: datetime) -> list:
        day_seed = int(day.strftime("%Y%m%d"))
        progs = list(self.programs)
        if self.shuffle:
            random.Random(self.seed ^ day_seed).shuffle(progs)
        if not progs:
            return []
        rng = random.Random(self.seed ^ day_seed ^ 0xC0FFEE)
        timeline, t, pi, since = [], 0.0, 0, 0.0
        DAY = 86400
        while t < DAY:
            prog = progs[pi % len(progs)]; pi += 1
            timeline.append((t, prog)); t += prog.duration; since += prog.duration
            if (self.commercial_break_every > 0 and self.commercials
                    and since >= self.commercial_break_every and t < DAY):
                for ad in self._ad_break(rng, 1e9):
                    timeline.append((t, ad)); t += ad.duration
                    if t >= DAY:
                        break
                since = 0.0
        return timeline

    def _grid_day(self, day: datetime) -> list:
        day_seed = int(day.strftime("%Y%m%d"))
        rng = random.Random(self.seed ^ day_seed ^ 0xBEEF)
        DAY = 86400

        todays = [Slot(**s) if isinstance(s, dict) else s for s in self.slots]
        todays = [s for s in todays if s.matches_day(day)]
        todays.sort(key=lambda s: s.start_seconds())

        epoch = datetime(2000, 1, 3)  # a Monday
        def show_index(slot: Slot) -> int:
            days_elapsed = (day.date() - epoch.date()).days
            return sum(1 for d in range(days_elapsed)
                       if slot.matches_day(epoch + timedelta(days=d)))

        placed = []
        for slot in todays:
            start = slot.start_seconds()
            ep = self._pick_sequential(slot.show, show_index(slot))
            if not ep:
                continue
            block = (slot.block * 60) if slot.block else ep.duration
            placed.append((start, min(start + block, DAY), ep))
        placed.sort()
        resolved = []
        for i, (s, e, item) in enumerate(placed):
            nxt = placed[i + 1][0] if i + 1 < len(placed) else DAY
            resolved.append((s, min(e, nxt), item))

        timeline = []
        filler_pool = self._episodes_of(None)
        fi = (day_seed % max(1, len(filler_pool))) if filler_pool else 0
        cursor = 0.0

        def fill(gap_start, gap_end):
            nonlocal fi
            t = gap_start
            while t < gap_end - 1 and filler_pool:
                item = filler_pool[fi % len(filler_pool)]; fi += 1
                timeline.append((t, item)); t += item.duration
                if self.commercials and self.commercial_break_every > 0:
                    for ad in self._ad_break(rng, gap_end - t):
                        if t >= gap_end:
                            break
                        timeline.append((t, ad)); t += ad.duration

        for (s, e, item) in resolved:
            if s > cursor:
                fill(cursor, s)
            timeline.append((s, item))
            content_end = s + item.duration
            if e > content_end:
                fill(content_end, e)
            cursor = max(cursor, e)
        if cursor < DAY:
            fill(cursor, DAY)
        timeline.sort(key=lambda x: x[0])
        return timeline

    def day_schedule(self, day: datetime) -> list:
        day = day.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.mode == "grid" and self.slots:
            return self._grid_day(day)
        return self._shuffle_day(day)

    def _in_signoff(self, now: datetime) -> bool:
        if not self.sign_off_start or not self.sign_off_end:
            return False
        s = dtime.fromisoformat(self.sign_off_start)
        e = dtime.fromisoformat(self.sign_off_end)
        cur = now.time()
        return (s <= cur < e) if s <= e else (cur >= s or cur < e)

    def now_playing(self, now: Optional[datetime] = None) -> dict:
        now = now or datetime.now()
        if self._in_signoff(now):
            return {"channel": self.number, "name": self.name,
                    "status": "sign_off", "title": "Sign-Off / Color Bars",
                    "seek": 0.0, "remaining": 0.0}
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        secs = (now - midnight).total_seconds()
        tl = self.day_schedule(midnight)
        if not tl:
            return {"channel": self.number, "name": self.name,
                    "status": "no_content", "title": "No Content",
                    "seek": 0.0, "remaining": 0.0}
        cur_start, cur = tl[0]
        for i, (start, item) in enumerate(tl):
            nxt = tl[i + 1][0] if i + 1 < len(tl) else 86400
            if start <= secs < nxt:
                cur_start, cur = start, item
                break
        else:
            cur_start, cur = tl[-1]
        seek = secs - cur_start
        return {
            "channel": self.number, "name": self.name, "status": "on_air",
            "kind": cur.kind, "title": cur.title, "show": cur.show,
            "source_kind": cur.source_kind, "stream_url": cur.stream_url(),
            "seek": round(seek, 2),
            "remaining": round(max(0.0, cur.duration - seek), 2),
            "duration": round(cur.duration, 2),
        }

    def upcoming(self, now: Optional[datetime] = None, count: int = 5) -> list:
        now = now or datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        secs = (now - midnight).total_seconds()
        out = []
        for start, item in self.day_schedule(midnight):
            if start >= secs and item.kind == "program":
                out.append({
                    "time": (midnight + timedelta(seconds=start)).strftime("%H:%M"),
                    "title": item.title, "duration": round(item.duration, 2)})
            if len(out) >= count:
                break
        return out


class Station:
    def __init__(self, config_path: str = "station.json"):
        self.config_path = config_path
        self.channels: dict = {}
        self.plex_servers: dict = {}

    def add_plex_server(self, name: str, base_url: str, token: str):
        self.plex_servers[name] = PlexServer(base_url, token)

    def add_channel(self, ch: Channel):
        ch.scan(self.plex_servers)
        self.channels[ch.number] = ch

    def guide(self, now: Optional[datetime] = None) -> list:
        now = now or datetime.now()
        rows = []
        for num in sorted(self.channels):
            np = self.channels[num].now_playing(now)
            np["up_next"] = self.channels[num].upcoming(now, 3)
            rows.append(np)
        return rows

    def save(self):
        data = {"plex_servers": {}, "channels": []}
        for name, srv in self.plex_servers.items():
            data["plex_servers"][name] = {"base_url": srv.base_url, "token": srv.token}
        for ch in self.channels.values():
            d = asdict(ch); d.pop("programs", None); d.pop("commercials", None)
            data["channels"].append(d)
        Path(self.config_path).write_text(json.dumps(data, indent=2))

    def load(self):
        data = json.loads(Path(self.config_path).read_text())
        for name, s in data.get("plex_servers", {}).items():
            self.add_plex_server(name, s["base_url"], s["token"])
        for d in data["channels"]:
            d.pop("programs", None); d.pop("commercials", None)
            self.add_channel(Channel(**d))
