"""
app/core/streamer.py — The gapless continuous streaming engine.

ARCHITECTURE (and why it's built this way)

An IPTV client (Plex Live TV, Jellyfin, TiViMate) expects a channel to be an
endless MPEG-TS stream. If the stream ever EOFs, the client disconnects. So per
channel we run ONE long-lived ffmpeg process that never sees end-of-input.

We feed it with the concat demuxer reading a playlist file:

    file '/media/ep1.mkv'
    file '/media/commercial.mp4'
    file '/media/ep2.mkv'
    ...

The well-known gotcha: ffmpeg's concat demuxer reads the playlist when the input
is OPENED. Lines appended later are not reliably picked up mid-stream across
ffmpeg builds. Two robust ways around it:

  (A) Pre-write a long playlist (hours ahead) and have a background "refiller"
      keep extending it well before playback reaches the end, combined with the
      concat demuxer. In practice ffmpeg's concat will continue reading new
      'file' lines as long as they are present before it hits EOF, when the
      playlist is provided via a pipe or kept ahead. To make this deterministic
      across builds we instead use approach (B).

  (B) PIPE approach: ffmpeg reads concat from a long playlist we generate to
      cover a big window (e.g. the next N hours from the schedule), and we
      RESTART seamlessly by chaining: when the window is nearly consumed we
      regenerate the next window. To avoid a visible gap on regeneration we keep
      the playlist long (hours), so regenerations are rare and happen far from
      the play head.

This module implements (B): a ChannelStream owns one ffmpeg process reading a
playlist that the refiller keeps extended to always cover >= REFILL_AHEAD secs
of upcoming schedule. The schedule itself is deterministic (see services/
scheduler.py), so "what plays now" is reproducible and the playlist is just a
materialization of it.

The HTTP layer (api/stream.py) attaches to the ffmpeg stdout and streams those
bytes to every connected client.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from app.models import FFmpegProfile
from app.core.ffmpeg_cmd import build_stream_cmd

UTC = timezone.utc

# Keep the playlist covering at least this many seconds ahead of the play head.
REFILL_AHEAD = 6 * 3600          # 6 hours
REFILL_INTERVAL = 60             # check/extend once a minute


@dataclass
class PlaylistEntry:
    path: str
    duration: float


@dataclass
class ChannelStream:
    """One long-lived ffmpeg process per channel, fed by a concat playlist."""
    channel_id: int
    profile: FFmpegProfile
    # callback(now_utc, covered_until_utc) -> list[PlaylistEntry]
    # returns scheduled items to append so coverage reaches >= REFILL_AHEAD.
    fetch_upcoming: Callable[[datetime, datetime], List[PlaylistEntry]]

    playlist_path: str = ""
    proc: Optional[subprocess.Popen] = None
    covered_until: datetime = field(default_factory=lambda: datetime.now(UTC))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop: threading.Event = field(default_factory=threading.Event)
    _refiller: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- helpers #
    def _write_initial_playlist(self):
        fd, self.playlist_path = tempfile.mkstemp(
            prefix=f"fs_ch{self.channel_id}_", suffix=".txt")
        os.close(fd)
        self.covered_until = datetime.now(UTC)
        self._extend()

    def _append_entries(self, entries: List[PlaylistEntry]):
        if not entries:
            return
        with open(self.playlist_path, "a") as f:
            for e in entries:
                # concat demuxer format; escape single quotes in paths
                safe = e.path.replace("'", "'\\''")
                f.write(f"file '{safe}'\n")
        for e in entries:
            self.covered_until += timedelta(seconds=e.duration)

    def _extend(self):
        """Top up the playlist so it covers >= REFILL_AHEAD secs ahead."""
        with self._lock:
            now = datetime.now(UTC)
            target = now + timedelta(seconds=REFILL_AHEAD)
            if self.covered_until >= target:
                return
            entries = self.fetch_upcoming(self.covered_until, target)
            self._append_entries(entries)

    def _refill_loop(self):
        while not self._stop.is_set():
            try:
                self._extend()
            except Exception:
                pass
            self._stop.wait(REFILL_INTERVAL)

    # ------------------------------------------------------------------ start #
    def start(self):
        self._write_initial_playlist()
        cmd = build_stream_cmd(self.profile, self.playlist_path)
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0)
        self._refiller = threading.Thread(target=self._refill_loop, daemon=True)
        self._refiller.start()
        return self

    def read(self, chunk: int = 65536) -> bytes:
        if not self.proc or not self.proc.stdout:
            return b""
        return self.proc.stdout.read(chunk)

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        if self.playlist_path and os.path.exists(self.playlist_path):
            try:
                os.remove(self.playlist_path)
            except OSError:
                pass

    def command_preview(self) -> str:
        return " ".join(shlex.quote(c)
                        for c in build_stream_cmd(self.profile, self.playlist_path))


class StreamManager:
    """Owns the live ChannelStream objects; one per channel, started on demand."""

    def __init__(self):
        self.streams: dict[int, ChannelStream] = {}
        self.lock = threading.Lock()

    def get_or_start(self, channel_id: int, profile: FFmpegProfile,
                     fetch_upcoming) -> ChannelStream:
        with self.lock:
            s = self.streams.get(channel_id)
            if s and s.alive():
                return s
            if s:
                s.stop()
            s = ChannelStream(channel_id=channel_id, profile=profile,
                              fetch_upcoming=fetch_upcoming).start()
            self.streams[channel_id] = s
            return s

    def stop(self, channel_id: int):
        with self.lock:
            s = self.streams.pop(channel_id, None)
        if s:
            s.stop()

    def stop_all(self):
        with self.lock:
            ids = list(self.streams)
        for cid in ids:
            self.stop(cid)
