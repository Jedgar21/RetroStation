"""
app/core/ffmpeg_cmd.py — Build FFmpeg argument lists from an FFmpegProfile.

This is the single source of truth for the transcode flags, including the three
hardware-acceleration paths. It is deliberately pure (no process management) so
it can be unit-tested without spawning ffmpeg.

The streaming engine (core/streamer.py) calls build_stream_cmd() with a concat
input and pipes the output to an HTTP response / mpegts mux.

Aspect ratio handling for retro content:
  We scale to fit the profile resolution while preserving the source aspect, pad
  to the target frame, and set the display aspect ratio (DAR). For 4:3 retro
  channels this pillarboxes widescreen sources correctly instead of stretching.
"""

from __future__ import annotations
from typing import List

from app.models import FFmpegProfile, HWAccel


def _scale_pad_filter(resolution: str, aspect: str, hw: HWAccel) -> str:
    """
    Build a -vf/-filter chain that fits the source into the target frame,
    preserves aspect with padding, and stamps the display aspect ratio.
    """
    w, h = resolution.split("x")
    setdar = "4/3" if aspect == "4:3" else "16/9"

    if hw == HWAccel.vaapi:
        # VAAPI uses GPU scaling; frames arrive as vaapi surfaces.
        # scale_vaapi handles resize on-GPU; padding on-GPU is limited, so we
        # scale to fit and rely on the encoder's target size.
        return (f"scale_vaapi=w={w}:h={h}:force_original_aspect_ratio=decrease,"
                f"setdar={setdar}")
    # CPU/NVENC/QSV(software-upload) path: scale fit + pad + setdar
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,setdar={setdar}")


def video_encoder_args(p: FFmpegProfile) -> List[str]:
    """The -c:v and codec-specific rate-control args for the profile."""
    br, maxrate = p.video_bitrate, p.video_bitrate
    bufsize = f"{int(p.video_bitrate.rstrip('k') or 3500) * 2}k"
    gop = str(p.fps * p.gop_seconds)

    if p.hwaccel == HWAccel.vaapi:
        return ["-c:v", "h264_vaapi", "-b:v", br, "-maxrate", maxrate,
                "-bufsize", bufsize, "-g", gop]
    if p.hwaccel == HWAccel.qsv:
        return ["-c:v", "h264_qsv", "-b:v", br, "-maxrate", maxrate,
                "-bufsize", bufsize, "-g", gop, "-look_ahead", "0"]
    if p.hwaccel == HWAccel.nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll",
                "-b:v", br, "-maxrate", maxrate, "-bufsize", bufsize, "-g", gop]
    # software
    return ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
            "-pix_fmt", "yuv420p", "-b:v", br, "-maxrate", maxrate,
            "-bufsize", bufsize, "-g", gop, "-sc_threshold", "0"]


def hwaccel_input_args(p: FFmpegProfile) -> List[str]:
    """Flags that must precede -i for the chosen hwaccel."""
    if p.hwaccel == HWAccel.vaapi:
        dev = p.hwaccel_device or "/dev/dri/renderD128"
        return ["-hwaccel", "vaapi", "-hwaccel_device", dev,
                "-hwaccel_output_format", "vaapi"]
    if p.hwaccel == HWAccel.qsv:
        return ["-hwaccel", "qsv"]
    if p.hwaccel == HWAccel.nvenc:
        return ["-hwaccel", "cuda"]
    return []


def build_stream_cmd(profile: FFmpegProfile, concat_path: str,
                     loglevel: str = "error") -> List[str]:
    """
    Full ffmpeg command: read the dynamic concat playlist and emit a continuous
    MPEG-TS stream to stdout (pipe:1) for the channel's HTTP endpoint.

    The concat demuxer with -stream_loop and -f concat reads the playlist; we
    keep the playlist refilled ahead of playback (see core/streamer.py) so the
    stream never ends. -re paces output at realtime so we don't burn CPU racing
    ahead, which is what an IPTV client expects from a live channel.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", loglevel,
           "-fflags", "+genpts",
           "-re",                                  # realtime pacing
           *hwaccel_input_args(profile),
           "-f", "concat", "-safe", "0",
           "-i", concat_path]

    vf = _scale_pad_filter(profile.resolution, profile.aspect_ratio,
                           profile.hwaccel)
    # VAAPI needs the upload/format step; software path uses -vf directly.
    if profile.hwaccel == HWAccel.vaapi:
        cmd += ["-vf", f"format=nv12|vaapi,hwupload,{vf}"]
    else:
        cmd += ["-vf", vf]

    cmd += video_encoder_args(profile)
    cmd += ["-c:a", profile.audio_codec, "-b:a", profile.audio_bitrate,
            "-ac", "2", "-r", str(profile.fps)]

    # continuous MPEG-TS to stdout for the HTTP muxer
    cmd += ["-f", "mpegts", "-mpegts_flags", "+resend_headers", "pipe:1"]
    return cmd
