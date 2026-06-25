#!/usr/bin/env python3
"""
Tier 1 — Scheduling logic tests. No media, no ffmpeg, no Plex.

Verifies the deterministic broadcast model: same wall-clock time -> same program
at the same offset; grid slots air the right show at the right time; episodes
advance sequentially across days; sign-off windows work.

Run:  python -m pytest tests/test_scheduling.py -v
  or: python tests/test_scheduling.py
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sources
from station import Channel, Slot


# --------------------------------------------------------------------------- #
# helpers to build fake content pools (no real files)
# --------------------------------------------------------------------------- #
def fake_programs(n, base=1200, step=30, title="Prog"):
    return [sources.MediaItem(title=f"{title} {i}", duration=base + i * step)
            for i in range(n)]


def fake_show(name, count, dur=1320):
    return [sources.MediaItem(title=f"{name} S1E{i}", duration=dur, show=name,
                              season=1, episode=i) for i in range(1, count + 1)]


def fake_ads(n=3, dur=30):
    return [sources.MediaItem(title=f"Ad {i}", duration=dur, kind="commercial")
            for i in range(n)]


def make_shuffle_channel():
    ch = Channel(number=2, name="SHUFFLE TV", mode="shuffle", seed=7)
    ch.programs = fake_programs(8)
    ch.commercials = fake_ads()
    return ch


def make_grid_channel():
    ch = Channel(number=5, name="GRID TV", mode="grid", seed=11)
    ch.programs = (fake_show("The News", 40, dur=1740)
                   + fake_show("Sitcom", 25, dur=1320)
                   + fake_programs(5, base=900, title="Filler"))
    ch.commercials = fake_ads()
    ch.slots = [
        Slot(days=["mon", "tue", "wed", "thu", "fri"], start="18:00",
             show="The News", block=30).__dict__,
        Slot(days=["mon", "tue", "wed", "thu", "fri"], start="18:30",
             show="Sitcom", block=30).__dict__,
    ]
    return ch


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_shuffle_now_playing_basic():
    ch = make_shuffle_channel()
    np = ch.now_playing(datetime(2026, 6, 25, 14, 7, 33))
    assert np["status"] == "on_air"
    assert np["title"]
    assert 0 <= np["seek"] <= np["duration"]
    assert np["remaining"] >= 0


def test_shuffle_is_deterministic():
    """Same wall-clock time must always yield the same program + offset."""
    ch = make_shuffle_channel()
    t = datetime(2026, 6, 25, 14, 7, 33)
    a = ch.now_playing(t)
    b = ch.now_playing(t)
    assert a == b


def test_offset_advances_with_time():
    """Two seconds later, the seek offset advances ~2s on the same program."""
    ch = make_shuffle_channel()
    t1 = datetime(2026, 6, 25, 14, 7, 33)
    t2 = datetime(2026, 6, 25, 14, 7, 35)
    a, b = ch.now_playing(t1), ch.now_playing(t2)
    if a["title"] == b["title"]:                       # not a program boundary
        assert abs((b["seek"] - a["seek"]) - 2.0) < 0.5


def test_schedule_fills_24h():
    ch = make_shuffle_channel()
    tl = ch.day_schedule(datetime(2026, 6, 25))
    assert tl[0][0] == 0.0                              # starts at midnight
    last_start, last_item = tl[-1]
    assert last_start + last_item.duration >= 86400     # covers full day


def test_upcoming_returns_future_programs():
    ch = make_shuffle_channel()
    t = datetime(2026, 6, 25, 14, 0, 0)
    up = ch.upcoming(t, count=3)
    assert len(up) == 3
    assert all("time" in u and "title" in u for u in up)


def test_grid_airs_correct_show_at_slot_time():
    ch = make_grid_channel()
    # Thursday 2026-06-25 18:05 -> The News slot
    np = ch.now_playing(datetime(2026, 6, 25, 18, 5, 0))
    assert np["show"] == "The News", f"got {np['show']}"
    # 18:32 -> Sitcom slot
    np2 = ch.now_playing(datetime(2026, 6, 25, 18, 32, 0))
    assert np2["show"] == "Sitcom", f"got {np2['show']}"


def test_grid_episodes_advance_across_days():
    """A daily slot should air a later episode each successive airing day."""
    ch = make_grid_channel()
    thu = ch.now_playing(datetime(2026, 6, 25, 18, 5, 0))["title"]   # Thu
    fri = ch.now_playing(datetime(2026, 6, 26, 18, 5, 0))["title"]   # Fri
    assert thu != fri, f"episode did not advance: {thu} == {fri}"


def test_grid_slot_seek_is_relative_to_slot_start():
    ch = make_grid_channel()
    # 5 minutes into the 18:00 slot -> seek ~300s
    np = ch.now_playing(datetime(2026, 6, 25, 18, 5, 0))
    assert abs(np["seek"] - 300) < 5, f"seek={np['seek']}"


def test_grid_weekend_has_no_weekday_slots():
    """Slots are Mon-Fri; Saturday 18:05 should fall to filler, not The News."""
    ch = make_grid_channel()
    np = ch.now_playing(datetime(2026, 6, 27, 18, 5, 0))   # Saturday
    assert np["show"] != "The News"


def test_signoff_window():
    ch = make_shuffle_channel()
    ch.sign_off_start = "02:00"
    ch.sign_off_end = "06:00"
    assert ch.now_playing(datetime(2026, 6, 25, 3, 0, 0))["status"] == "sign_off"
    assert ch.now_playing(datetime(2026, 6, 25, 12, 0, 0))["status"] == "on_air"


def test_signoff_crossing_midnight():
    ch = make_shuffle_channel()
    ch.sign_off_start = "23:00"
    ch.sign_off_end = "05:00"
    assert ch.now_playing(datetime(2026, 6, 25, 23, 30))["status"] == "sign_off"
    assert ch.now_playing(datetime(2026, 6, 25, 1, 0))["status"] == "sign_off"
    assert ch.now_playing(datetime(2026, 6, 25, 12, 0))["status"] == "on_air"


def test_empty_channel_reports_no_content():
    ch = Channel(number=9, name="EMPTY", mode="shuffle")
    np = ch.now_playing(datetime(2026, 6, 25, 12, 0))
    assert np["status"] == "no_content"


def test_commercial_breaks_injected():
    ch = make_shuffle_channel()
    ch.commercial_break_every = 600       # break after every 10 min of program
    tl = ch.day_schedule(datetime(2026, 6, 25))
    kinds = {item.kind for _, item in tl}
    assert "commercial" in kinds


# --------------------------------------------------------------------------- #
# allow running without pytest
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
