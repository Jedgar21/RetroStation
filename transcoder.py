#!/usr/bin/env python3
"""
Live HLS transcoder for RetroStation.

Given a media stream URL/path and a seek offset, spawn ffmpeg to produce an
HLS playlist (H.264/AAC) starting at that offset, so any source codec plays in
any browser and you join the broadcast already in progress.

Design:
  - One ffmpeg process per active channel, writing to a temp HLS dir.
  - The playlist is a short sliding window (live), so memory/disk stay bounded.
  - When the airing item changes (program ends, ad starts), the channel's
    process is restarted at the new item + offset. The HLS discontinuity is
    handled by tearing down and relaunching under the same channel id.
  - If a stream is already H.264/AAC in an MP4-friendly container, we still
    remux through HLS for uniform seeking; copy codecs when possible to save CPU.
"""

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class ChannelStream:
    channel: int
    proc: subprocess.Popen
    hls_dir: str
    item_title: str
    started_at: float
    seek: float


class Transcoder:
    """Manages per-channel ffmpeg HLS sessions."""

    def __init__(self, work_root: Optional[str] = None,
                 hls_time: int = 4, window: int = 8,
                 video_bitrate: str = "3500k", try_copy: bool = True,
                 encoder: str = "auto", encoder_device: Optional[str] = None,
                 preset: Optional[str] = None, verify_encoder: bool = True):
        self.root = work_root or tempfile.mkdtemp(prefix="retrostation_hls_")
        self.hls_time = hls_time          # segment length (s)
        self.window = window              # segments kept in the live playlist
        self.video_bitrate = video_bitrate
        self.try_copy = try_copy
        self.sessions: dict[int, ChannelStream] = {}
        self.lock = threading.Lock()
        os.makedirs(self.root, exist_ok=True)

        # Resolve the H.264 encoder backend (hardware-accelerated or cpu).
        from encoders import resolve_encoder, EncodeProfile
        self.encoder = resolve_encoder(encoder, device=encoder_device,
                                       verify=verify_encoder)
        bitrate_num = int(video_bitrate.rstrip("k") or "3500")
        self.profile = EncodeProfile(
            video_bitrate=video_bitrate, maxrate=video_bitrate,
            bufsize=f"{bitrate_num * 2}k",
            gop=hls_time * 30, preset=preset)
        note = getattr(self.encoder, "fallback_note", None)
        print(f"[transcoder] {note}" if note
              else f"[transcoder] using encoder: {self.encoder.name}")

    # ----------------------------------------------------------------------- #
    def _channel_dir(self, channel: int) -> str:
        d = os.path.join(self.root, f"ch{channel}")
        os.makedirs(d, exist_ok=True)
        return d

    def _build_cmd(self, src: str, seek: float, out_dir: str,
                   copy_ok: bool) -> list:
        playlist = os.path.join(out_dir, "index.m3u8")
        seg = os.path.join(out_dir, "seg_%05d.ts")

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]

        if copy_ok:
            # remux only -- cheapest path when already h264/aac, no HW needed.
            # -ss before -i = fast input seek (keyframe accurate enough for TV).
            cmd += ["-ss", f"{seek:.3f}", "-i", src, "-c", "copy"]
        else:
            # Hardware/software encode via the selected backend. Input-side
            # hwaccel flags must precede -i; the seek stays before -i too.
            cmd += self.encoder.input_flags()
            cmd += ["-ss", f"{seek:.3f}", "-i", src]
            vf = self.encoder.video_filter()
            if vf:
                cmd += ["-vf", vf]
            cmd += self.encoder.video_args(self.profile)
            cmd += self.encoder.audio_args(self.profile)

        cmd += [
            "-f", "hls",
            "-hls_time", str(self.hls_time),
            "-hls_list_size", str(self.window),
            "-hls_flags", "delete_segments+append_list+omit_endlist",
            "-hls_segment_filename", seg,
            playlist,
        ]
        return cmd

    def _probe_codecs(self, src: str) -> tuple:
        """Return (vcodec, acodec) or (None, None) if probing fails."""
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_name,codec_type", "-of",
                 "default=noprint_wrappers=1", src],
                capture_output=True, text=True, timeout=20).stdout
            v = a = None
            cur = {}
            for line in out.splitlines():
                if "=" in line:
                    k, val = line.split("=", 1)
                    cur[k] = val
                if cur.get("codec_type") == "video" and "codec_name" in cur:
                    v = cur["codec_name"]; cur = {}
                elif cur.get("codec_type") == "audio" and "codec_name" in cur:
                    a = cur["codec_name"]; cur = {}
            return v, a
        except Exception:
            return None, None

    # ----------------------------------------------------------------------- #
    def start(self, channel: int, src: str, seek: float,
              item_title: str) -> str:
        """(Re)start a channel's HLS session. Returns the playlist path."""
        with self.lock:
            self._stop_locked(channel)
            out_dir = self._channel_dir(channel)
            # clear old segments
            for f in os.listdir(out_dir):
                try:
                    os.remove(os.path.join(out_dir, f))
                except OSError:
                    pass

            copy_ok = False
            if self.try_copy:
                v, a = self._probe_codecs(src)
                copy_ok = (v == "h264" and a in ("aac", "mp3"))

            cmd = self._build_cmd(src, seek, out_dir, copy_ok)
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            self.sessions[channel] = ChannelStream(
                channel=channel, proc=proc, hls_dir=out_dir,
                item_title=item_title, started_at=time.time(), seek=seek)
            return os.path.join(out_dir, "index.m3u8")

    def ensure(self, channel: int, src: str, seek: float,
               item_title: str, drift_tolerance: float = 6.0) -> str:
        """
        Ensure a live session exists for the channel airing `item_title`.
        Restarts only when the program changed -- ongoing playback is left alone
        so viewers don't get yanked back every poll.
        """
        with self.lock:
            s = self.sessions.get(channel)
            same_item = s and s.item_title == item_title
            if same_item:
                running = s.proc.poll() is None
                # A finished encode is still usable as long as its playlist is
                # intact: short programs (or fast remuxes) can complete before
                # the next poll, and we must not yank the viewer back to start.
                pl = os.path.join(s.hls_dir, "index.m3u8")
                intact = os.path.exists(pl) and os.path.getsize(pl) > 0
                if running or intact:
                    return pl
        # different item, or no usable session -> (re)start
        return self.start(channel, src, seek, item_title)

    def playlist_ready(self, channel: int, timeout: float = 12.0) -> bool:
        """Block until the first segment exists (or timeout)."""
        s = self.sessions.get(channel)
        if not s:
            return False
        pl = os.path.join(s.hls_dir, "index.m3u8")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(pl) and os.path.getsize(pl) > 0:
                # also wait for at least one segment
                segs = [f for f in os.listdir(s.hls_dir) if f.endswith(".ts")]
                if segs:
                    return True
            if s.proc.poll() is not None:   # ffmpeg died
                return False
            time.sleep(0.25)
        return False

    # ----------------------------------------------------------------------- #
    def _stop_locked(self, channel: int):
        s = self.sessions.pop(channel, None)
        if s and s.proc.poll() is None:
            try:
                s.proc.send_signal(signal.SIGTERM)
                s.proc.wait(timeout=5)
            except Exception:
                try:
                    s.proc.kill()
                except Exception:
                    pass

    def stop(self, channel: int):
        with self.lock:
            self._stop_locked(channel)

    def stop_all(self):
        with self.lock:
            for ch in list(self.sessions):
                self._stop_locked(ch)

    def cleanup(self):
        self.stop_all()
        shutil.rmtree(self.root, ignore_errors=True)
