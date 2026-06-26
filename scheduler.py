"""
app/services/scheduler.py — expand schedule blocks into concrete slots, and
answer the streamer's playlist-window queries.

A ScheduleBlock is the high-level directive the user builds in the UI (drop a
show/collection onto a time range, choose how it plays, choose commercials).
expand_block() turns one block into ScheduleSlot rows:

  rule:
    chronological  sequential episodes/files (collections in their order)
    shuffle        randomized, seeded by block start (deterministic)
    specific       repeat one chosen media item
    block          same as chronological for non-series pools

  commercials:
    placement before|after|middle, drawn from a commercial collection or a
    media_type pool, padding up to commercial_pad_minutes between content.
"""

import random
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlmodel import Session, select

from app.models import (MediaItem, MediaType, ScheduleSlot, ScheduleBlock,
                        ScheduleRule, CommercialPlacement)
from app.services.collections import resolve_collection
from app.core.streamer import PlaylistEntry

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# content + commercial pools
# --------------------------------------------------------------------------- #
def _content_pool(session: Session, block: ScheduleBlock) -> List[MediaItem]:
    if block.collection_id:
        return resolve_collection(session, block.collection_id)
    if block.show_title:
        eps = session.exec(select(MediaItem).where(
            MediaItem.show_title == block.show_title)).all()
        eps.sort(key=lambda m: (m.season or 0, m.episode or 0, m.title))
        return eps
    if block.media_id:
        m = session.get(MediaItem, block.media_id)
        return [m] if m else []
    if block.media_type:
        return session.exec(select(MediaItem).where(
            MediaItem.media_type == block.media_type)).all()
    return []


def _commercial_pool(session: Session, block: ScheduleBlock) -> List[MediaItem]:
    if block.commercial_collection_id:
        return resolve_collection(session, block.commercial_collection_id)
    mt = block.commercial_media_type or MediaType.commercial
    return session.exec(select(MediaItem).where(
        MediaItem.media_type == mt)).all()


def _ad_run(rng, ads: List[MediaItem], pad_seconds: float) -> List[MediaItem]:
    """Pick commercials filling up to pad_seconds."""
    if not ads or pad_seconds <= 0:
        return []
    out, used = [], 0.0
    while used < pad_seconds:
        ad = rng.choice(ads)
        if not ad.duration:
            continue
        out.append(ad)
        used += ad.duration
        if len(out) > 200:
            break
    return out


# --------------------------------------------------------------------------- #
# block expansion
# --------------------------------------------------------------------------- #
def expand_block(session: Session, block: ScheduleBlock) -> List[ScheduleSlot]:
    pool = [m for m in _content_pool(session, block) if m and m.duration]
    if not pool:
        return []

    if block.rule == ScheduleRule.shuffle:
        random.Random(int(block.start_time.timestamp()) ^ block.channel_id).shuffle(pool)
    elif block.rule == ScheduleRule.specific and block.specific_media_id:
        one = session.get(MediaItem, block.specific_media_id)
        pool = [one] if one and one.duration else pool

    ads = _commercial_pool(session, block)
    rng = random.Random(int(block.start_time.timestamp()) ^ 0xAD ^ block.channel_id)
    pad_seconds = block.commercial_pad_minutes * 60
    placement = block.commercial_placement

    slots: List[ScheduleSlot] = []
    cursor = block.start_time
    end = block.end_time
    i = 0

    def emit(item: MediaItem):
        nonlocal cursor
        if cursor >= end:
            return False
        dur = timedelta(seconds=item.duration)
        slot_end = min(cursor + dur, end)
        slots.append(ScheduleSlot(
            channel_id=block.channel_id, media_id=item.id,
            start_time=cursor, end_time=slot_end,
            rule=block.rule, block_id=block.id))
        cursor = slot_end
        return cursor < end

    while cursor < end:
        item = pool[i % len(pool)]
        i += 1

        if placement == CommercialPlacement.before:
            for ad in _ad_run(rng, ads, pad_seconds):
                if not emit(ad):
                    break
            if cursor >= end:
                break
            emit(item)
        elif placement == CommercialPlacement.after:
            emit(item)
            for ad in _ad_run(rng, ads, pad_seconds):
                if not emit(ad):
                    break
        elif placement == CommercialPlacement.middle:
            # split: half the program, ads, second half (approximate via two slots)
            half = timedelta(seconds=item.duration / 2)
            if cursor + half < end:
                mid_end = cursor + half
                slots.append(ScheduleSlot(
                    channel_id=block.channel_id, media_id=item.id,
                    start_time=cursor, end_time=mid_end,
                    rule=block.rule, block_id=block.id))
                cursor = mid_end
                for ad in _ad_run(rng, ads, pad_seconds):
                    if not emit(ad):
                        break
                # second half
                if cursor < end:
                    sec_end = min(cursor + half, end)
                    slots.append(ScheduleSlot(
                        channel_id=block.channel_id, media_id=item.id,
                        start_time=cursor, end_time=sec_end,
                        rule=block.rule, block_id=block.id))
                    cursor = sec_end
            else:
                emit(item)
        else:
            emit(item)

    return slots


def commit_slots(session: Session, slots: List[ScheduleSlot]):
    for s in slots:
        session.add(s)
    session.commit()


def clear_channel_range(session: Session, channel_id: int,
                        start: datetime, end: datetime):
    rows = session.exec(select(ScheduleSlot).where(
        ScheduleSlot.channel_id == channel_id,
        ScheduleSlot.start_time >= start,
        ScheduleSlot.start_time < end)).all()
    for r in rows:
        session.delete(r)
    session.commit()


# --------------------------------------------------------------------------- #
# playlist window for the streamer
# --------------------------------------------------------------------------- #
def playlist_window(session: Session, channel_id: int,
                    start: datetime, end: datetime) -> List[PlaylistEntry]:
    rows = session.exec(select(ScheduleSlot).where(
        ScheduleSlot.channel_id == channel_id,
        ScheduleSlot.end_time > start,
        ScheduleSlot.start_time < end)
        .order_by(ScheduleSlot.start_time)).all()

    entries: List[PlaylistEntry] = []
    for slot in rows:
        m = session.get(MediaItem, slot.media_id)
        if m and m.path:
            entries.append(PlaylistEntry(path=m.path, duration=m.duration or 0))
    if entries:
        return entries

    # fallback: loop whatever exists so the channel never goes dark
    any_slots = session.exec(select(ScheduleSlot).where(
        ScheduleSlot.channel_id == channel_id)
        .order_by(ScheduleSlot.start_time).limit(500)).all()
    need = (end - start).total_seconds()
    filled, i, cache = 0.0, 0, {}
    while filled < need and any_slots and i < 10000:
        slot = any_slots[i % len(any_slots)]; i += 1
        m = cache.get(slot.media_id) or session.get(MediaItem, slot.media_id)
        cache[slot.media_id] = m
        if m and m.path and m.duration:
            entries.append(PlaylistEntry(path=m.path, duration=m.duration))
            filled += m.duration
    return entries
