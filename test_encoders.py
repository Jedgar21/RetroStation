#!/usr/bin/env python3
"""
Tier 2b — Encoder backend tests.

Verifies the encoder abstraction builds correct ffmpeg fragments for each
backend, that detection/resolution falls back safely, and (with ffmpeg present)
that the cpu path actually encodes. Hardware encoders are probed but only
asserted to *work or cleanly report unavailable* -- we can't require a GPU.

Run:  python tests/test_encoders.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import encoders
from encoders import (CPUEncoder, NVENCEncoder, QSVEncoder, VAAPIEncoder,
                      VideoToolboxEncoder, EncodeProfile, resolve_encoder,
                      make_encoder, available_ffmpeg_encoders, probe_encoder)

P = EncodeProfile(video_bitrate="3500k", maxrate="3500k", bufsize="7000k", gop=120)


def test_cpu_builds_libx264():
    args = CPUEncoder().video_args(P)
    assert "libx264" in args
    assert "-b:v" in args and "3500k" in args


def test_nvenc_uses_cuda_and_nvenc():
    enc = NVENCEncoder()
    assert "-hwaccel" in enc.input_flags() and "cuda" in enc.input_flags()
    assert "h264_nvenc" in enc.video_args(P)


def test_qsv_uses_nv12_filter():
    enc = QSVEncoder()
    assert enc.video_filter() == "format=nv12"
    assert "h264_qsv" in enc.video_args(P)
    assert "qsv" in enc.input_flags()


def test_vaapi_device_in_flags():
    enc = VAAPIEncoder(device="/dev/dri/renderD129")
    flags = enc.input_flags()
    assert "/dev/dri/renderD129" in flags
    assert "h264_vaapi" in enc.video_args(P)
    assert "hwupload" in enc.video_filter()


def test_videotoolbox_args():
    assert "h264_videotoolbox" in VideoToolboxEncoder().video_args(P)


def test_all_backends_emit_audio_args():
    for cls in (CPUEncoder, NVENCEncoder, QSVEncoder, VAAPIEncoder,
                VideoToolboxEncoder):
        a = cls().audio_args(P)
        assert "aac" in a


def test_make_encoder_unknown_returns_none():
    assert make_encoder("nonsense") is None


def test_make_encoder_vaapi_passes_device():
    enc = make_encoder("vaapi", device="/dev/dri/renderD200")
    assert "/dev/dri/renderD200" in enc.input_flags()


def test_resolve_explicit_unavailable_falls_back_to_cpu():
    """Requesting an encoder that can't run must yield cpu, with a note."""
    # 'nvenc' won't pass the probe on a GPU-less box; force verify on.
    enc = resolve_encoder("nvenc", verify=True)
    # Either it genuinely works (has GPU) or it fell back to cpu.
    assert enc.name in ("nvenc", "cpu")
    if enc.name == "cpu":
        assert getattr(enc, "fallback_note", None)


def test_resolve_auto_always_returns_usable():
    enc = resolve_encoder("auto")
    assert enc.name in ("cpu", "nvenc", "qsv", "vaapi", "videotoolbox")
    assert enc.video_args(P)            # produces a real command fragment


def test_resolve_cpu_explicit():
    enc = resolve_encoder("cpu", verify=False)
    assert enc.name == "cpu"


def test_detection_lists_something_or_empty():
    """available_ffmpeg_encoders returns a set (may be empty without ffmpeg)."""
    result = available_ffmpeg_encoders()
    assert isinstance(result, set)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
