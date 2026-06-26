#!/usr/bin/env python3
"""
Encoder backends for RetroStation's transcoder.

Each backend knows how to build the ffmpeg argument fragments for a given
hardware (or software) H.264 encoder: any extra input-side flags (e.g. a
hwaccel device), the video codec args, and the pixel-format/scale filter needed
to feed that encoder. The transcoder stitches these into the full command.

Supported encoders:

  cpu          libx264         software, works everywhere (default fallback)
  nvenc        h264_nvenc      NVIDIA GPUs
  qsv          h264_qsv        Intel Quick Sync (integrated graphics)
  vaapi        h264_vaapi      VAAPI: Intel/AMD on Linux
  videotoolbox h264_videotoolbox  Apple Silicon / macOS

Design notes:
  - We only target H.264 output (universally browser-playable via HLS).
  - 'auto' detection asks ffmpeg which encoders were compiled in and picks the
    first hardware one available, else falls back to cpu. Note: a compiled-in
    encoder is necessary but not sufficient -- the device must also be present
    and passed into the container. Use `probe_encoder()` to actually test it.
  - Bitrate/quality knobs are normalized across backends as best we can.
"""

import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class EncodeProfile:
    """Normalized quality knobs the transcoder passes to a backend."""
    video_bitrate: str = "3500k"
    maxrate: str = "3500k"
    bufsize: str = "7000k"
    gop: int = 120              # keyframe interval (frames)
    preset: Optional[str] = None  # backend-specific; None = backend default
    audio_bitrate: str = "128k"


class Encoder:
    """Base class. Subclasses fill in the ffmpeg fragments."""
    name = "base"
    ffmpeg_encoder = None       # the -c:v value, used for availability checks

    def input_flags(self) -> list:
        """Flags that must appear BEFORE -i (hwaccel/device setup)."""
        return []

    def video_filter(self) -> Optional[str]:
        """A -vf filter string to format frames for this encoder, or None."""
        return "format=yuv420p"

    def video_args(self, p: EncodeProfile) -> list:
        """The -c:v and related rate-control args."""
        raise NotImplementedError

    def audio_args(self, p: EncodeProfile) -> list:
        return ["-c:a", "aac", "-b:a", p.audio_bitrate, "-ac", "2"]


class CPUEncoder(Encoder):
    name = "cpu"
    ffmpeg_encoder = "libx264"

    def video_args(self, p: EncodeProfile) -> list:
        return [
            "-c:v", "libx264", "-preset", p.preset or "veryfast",
            "-profile:v", "main", "-b:v", p.video_bitrate,
            "-maxrate", p.maxrate, "-bufsize", p.bufsize,
            "-g", str(p.gop), "-sc_threshold", "0",
        ]


class NVENCEncoder(Encoder):
    name = "nvenc"
    ffmpeg_encoder = "h264_nvenc"

    def input_flags(self) -> list:
        # CUDA hwaccel keeps frames on the GPU; decode + encode both on device.
        return ["-hwaccel", "cuda"]

    def video_args(self, p: EncodeProfile) -> list:
        return [
            "-c:v", "h264_nvenc", "-preset", p.preset or "p4",
            "-tune", "ll", "-profile:v", "main",
            "-b:v", p.video_bitrate, "-maxrate", p.maxrate,
            "-bufsize", p.bufsize, "-g", str(p.gop),
        ]


class QSVEncoder(Encoder):
    name = "qsv"
    ffmpeg_encoder = "h264_qsv"

    def input_flags(self) -> list:
        return ["-hwaccel", "qsv"]

    def video_filter(self) -> Optional[str]:
        # QSV wants nv12; software-side format conversion is fine here.
        return "format=nv12"

    def video_args(self, p: EncodeProfile) -> list:
        return [
            "-c:v", "h264_qsv", "-preset", p.preset or "veryfast",
            "-profile:v", "main", "-b:v", p.video_bitrate,
            "-maxrate", p.maxrate, "-bufsize", p.bufsize, "-g", str(p.gop),
        ]


class VAAPIEncoder(Encoder):
    name = "vaapi"
    ffmpeg_encoder = "h264_vaapi"

    def __init__(self, device: str = "/dev/dri/renderD128"):
        self.device = device

    def input_flags(self) -> list:
        # Initialize a VAAPI device and decode into it.
        return ["-vaapi_device", self.device,
                "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]

    def video_filter(self) -> Optional[str]:
        # Upload to GPU surfaces in nv12. (hwaccel_output_format already vaapi,
        # so format+hwupload ensures frames are in the right place.)
        return "format=nv12|vaapi,hwupload"

    def video_args(self, p: EncodeProfile) -> list:
        return [
            "-c:v", "h264_vaapi", "-profile:v", "main",
            "-b:v", p.video_bitrate, "-maxrate", p.maxrate,
            "-bufsize", p.bufsize, "-g", str(p.gop),
        ]


class VideoToolboxEncoder(Encoder):
    name = "videotoolbox"
    ffmpeg_encoder = "h264_videotoolbox"

    def video_args(self, p: EncodeProfile) -> list:
        return [
            "-c:v", "h264_videotoolbox", "-profile:v", "main",
            "-b:v", p.video_bitrate, "-maxrate", p.maxrate,
            "-bufsize", p.bufsize, "-g", str(p.gop),
        ]


# Registry of constructible backends.
_REGISTRY = {
    "cpu": CPUEncoder,
    "nvenc": NVENCEncoder,
    "qsv": QSVEncoder,
    "vaapi": VAAPIEncoder,
    "videotoolbox": VideoToolboxEncoder,
}

# Preference order when auto-detecting (fastest/most-common HW first, cpu last).
_AUTO_ORDER = ["nvenc", "qsv", "vaapi", "videotoolbox", "cpu"]


def available_ffmpeg_encoders() -> set:
    """Names of H.264 encoders compiled into the local ffmpeg."""
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=15).stdout
        return {line.split()[1] for line in out.splitlines()
                if len(line.split()) >= 2 and "264" in line.split()[1]}
    except Exception:
        return set()


def probe_encoder(name: str, device: Optional[str] = None) -> bool:
    """
    Actually try a 1-frame encode to confirm the encoder works on THIS machine
    (device present, drivers loaded, container passthrough correct). This is the
    only reliable check -- a compiled-in encoder can still fail at runtime.
    """
    enc = make_encoder(name, device=device)
    if enc is None:
        return False
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           *enc.input_flags(),
           "-f", "lavfi", "-i", "testsrc=duration=0.1:size=320x240:rate=5"]
    vf = enc.video_filter()
    if vf:
        cmd += ["-vf", vf]
    cmd += [*enc.video_args(EncodeProfile()), "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True,
                              timeout=30).returncode == 0
    except Exception:
        return False


def make_encoder(name: str, device: Optional[str] = None) -> Optional[Encoder]:
    """Construct a backend by name. Returns None if unknown."""
    cls = _REGISTRY.get(name)
    if cls is None:
        return None
    if name == "vaapi" and device:
        return cls(device=device)
    return cls()


def resolve_encoder(name: str = "auto", device: Optional[str] = None,
                    verify: bool = True) -> Encoder:
    """
    Turn a requested encoder name into a working Encoder instance.

      - explicit name ("nvenc"): build it; if verify and it fails the probe,
        fall back to cpu (with a note via the returned encoder's .fallback_note).
      - "auto": probe hardware encoders in preference order, pick the first that
        works, else cpu.

    Always returns a usable Encoder (cpu in the worst case).
    """
    compiled = available_ffmpeg_encoders()

    def _try(n):
        enc = make_encoder(n, device=device)
        if enc is None:
            return None
        if enc.ffmpeg_encoder and enc.ffmpeg_encoder not in compiled:
            return None
        if verify and n != "cpu" and not probe_encoder(n, device=device):
            return None
        return enc

    if name == "auto":
        for n in _AUTO_ORDER:
            enc = _try(n)
            if enc:
                enc.fallback_note = None if n == _AUTO_ORDER[0] else (
                    f"auto-selected '{n}'")
                return enc
        cpu = CPUEncoder(); cpu.fallback_note = "auto-selected 'cpu'"
        return cpu

    enc = _try(name)
    if enc:
        enc.fallback_note = None
        return enc
    cpu = CPUEncoder()
    cpu.fallback_note = (f"requested '{name}' but it isn't available here; "
                         f"falling back to cpu")
    return cpu


if __name__ == "__main__":
    # quick diagnostic: what's available on this box?
    print("Compiled H.264 encoders:", ", ".join(sorted(available_ffmpeg_encoders())) or "(none)")
    for n in _AUTO_ORDER:
        if n == "cpu":
            print(f"  {n:13} always available")
            continue
        ok = probe_encoder(n)
        print(f"  {n:13} {'WORKS' if ok else 'not available'}")
    picked = resolve_encoder("auto")
    print(f"\nauto -> {picked.name}")
