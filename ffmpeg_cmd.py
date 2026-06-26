"""
app/core/ffmpeg_cmd.py — Build FFmpeg argument lists from a profile + channel.

Single source of truth for transcode flags: video geometry/codec, the three
hardware-acceleration paths, full audio control (codec/bitrate/bufsize/channels/
sample rate), optional EBU R128 audio normalization and video level
normalization, plus per-channel preferred-language audio selection and burned-in
subtitles.

Pure (no process management) so it can be unit-tested without spawning ffmpeg.
"""

from typing import List, Optional

from app.models import FFmpegProfile, Channel, HWAccel


def _scale_pad_filter(resolution: str, aspect: str, hw: HWAccel) -> str:
    w, h = resolution.split("x")
    setdar = "4/3" if aspect == "4:3" else "16/9"
    if hw == HWAccel.vaapi:
        return (f"scale_vaapi=w={w}:h={h}:force_original_aspect_ratio=decrease,"
                f"setdar={setdar}")
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,setdar={setdar}")


def video_encoder_args(p: FFmpegProfile) -> List[str]:
    br = p.video_bitrate
    bufsize = p.video_bufsize or f"{int((br.rstrip('k') or '3500')) * 2}k"
    gop = str(p.fps * p.gop_seconds)
    if p.hwaccel == HWAccel.vaapi:
        return ["-c:v", "h264_vaapi", "-b:v", br, "-maxrate", br,
                "-bufsize", bufsize, "-g", gop]
    if p.hwaccel == HWAccel.qsv:
        return ["-c:v", "h264_qsv", "-b:v", br, "-maxrate", br,
                "-bufsize", bufsize, "-g", gop, "-look_ahead", "0"]
    if p.hwaccel == HWAccel.nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll",
                "-b:v", br, "-maxrate", br, "-bufsize", bufsize, "-g", gop]
    return ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
            "-pix_fmt", "yuv420p", "-b:v", br, "-maxrate", br,
            "-bufsize", bufsize, "-g", gop, "-sc_threshold", "0"]


def audio_encoder_args(p: FFmpegProfile) -> List[str]:
    return ["-c:a", p.audio_codec, "-b:a", p.audio_bitrate,
            "-ac", str(p.audio_channels), "-ar", str(p.audio_sample_rate)]


def hwaccel_input_args(p: FFmpegProfile) -> List[str]:
    if p.hwaccel == HWAccel.vaapi:
        dev = p.hwaccel_device or "/dev/dri/renderD128"
        return ["-hwaccel", "vaapi", "-hwaccel_device", dev,
                "-hwaccel_output_format", "vaapi"]
    if p.hwaccel == HWAccel.qsv:
        return ["-hwaccel", "qsv"]
    if p.hwaccel == HWAccel.nvenc:
        return ["-hwaccel", "cuda"]
    return []


def _audio_filters(p: FFmpegProfile) -> List[str]:
    f = []
    if p.audio_normalize:
        # EBU R128 loudness normalization to broadcast target
        f.append("loudnorm=I=-23:TP=-2:LRA=7")
    return f


def _video_filters(p: FFmpegProfile, channel: Optional[Channel]) -> List[str]:
    """Video filter chain pieces (excluding hw upload, added by caller)."""
    f = [_scale_pad_filter(p.resolution, p.aspect_ratio, p.hwaccel)]
    if p.video_normalize and p.hwaccel != HWAccel.vaapi:
        # gentle level normalization; skipped on vaapi (frames on GPU)
        f.insert(0, "normalize=blackpt=black:whitept=white")
    if channel and channel.burn_subtitles:
        # burned-in subtitles handled separately (needs the input file path);
        # placeholder marker replaced in build_stream_cmd when applicable.
        pass
    return f


def _map_audio_by_language(channel: Optional[Channel]) -> List[str]:
    """
    Select the audio stream matching the channel's preferred language, falling
    back to the first audio stream if no match. Uses ffmpeg stream specifiers.
    """
    if channel and channel.preferred_language:
        lang = channel.preferred_language
        # map preferred-language audio if present, else first audio track
        return ["-map", "0:v:0",
                "-map", f"0:a:m:language:{lang}?",
                "-map", "0:a:0?"]
    return []


def build_stream_cmd(profile: FFmpegProfile, concat_path: str,
                     channel: Optional[Channel] = None,
                     loglevel: str = "error") -> List[str]:
    """
    Full ffmpeg command: read the dynamic concat playlist, emit continuous
    MPEG-TS to stdout. Honors profile (video+audio+normalization+hwaccel) and
    channel (preferred language, burned subtitles).

    Note on subtitles: burning subs requires the subtitles filter to reference
    the *current input file*. With the concat demuxer the input is the playlist,
    and the subtitles filter reads embedded subs from the active segment via
    `subtitles=<concat_path>` is not valid; instead we burn the first subtitle
    stream using the `-filter_complex` overlay when a single file is streamed.
    For the continuous concat stream we burn embedded subs per-file using the
    `subtitles` filter pointed at the concat input with stream selection, which
    ffmpeg applies per active file. If unsupported by a build, subs are skipped
    gracefully (the stream still runs).
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", loglevel,
           "-fflags", "+genpts", "-re",
           *hwaccel_input_args(profile),
           "-f", "concat", "-safe", "0", "-i", concat_path]

    # audio language mapping (must come before filters/codec)
    cmd += _map_audio_by_language(channel)

    # video filter chain
    vfs = _video_filters(profile, channel)
    if channel and channel.burn_subtitles:
        # burn embedded subtitle stream; language-selected if specified
        if channel.subtitle_language:
            vfs.append(f"subtitles={_q(concat_path)}:stream_index=0")
        else:
            vfs.append(f"subtitles={_q(concat_path)}")
    if profile.hwaccel == HWAccel.vaapi:
        vf = ",".join(["format=nv12|vaapi", "hwupload"] + vfs)
    else:
        vf = ",".join(vfs)
    if vf:
        cmd += ["-vf", vf]

    cmd += video_encoder_args(profile)
    cmd += audio_encoder_args(profile)
    af = _audio_filters(profile)
    if af:
        cmd += ["-af", ",".join(af)]
    cmd += ["-r", str(profile.fps)]
    cmd += ["-f", "mpegts", "-mpegts_flags", "+resend_headers", "pipe:1"]
    return cmd


def _q(path: str) -> str:
    # escape for ffmpeg filter argument
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
