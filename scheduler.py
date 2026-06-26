"""
app/services/scheduler.py — Turns scheduling rules into concrete ScheduleSlots,
and answers the streamer's "what plays from time X to Y" refill queries.

The schedule is the source of truth: a row per (channel, media, start, end). The
drag-and-drop UI creates "directives" (e.g. "Mon-Fri 16:00, Cartoons,
chronological, 2h block") which this service expands into ScheduleSlot rows for
the next N days. The streamer then materializes slots into the concat playlist.

Two responsibilities:
  generate_slots(...)  expand a directive into ScheduleSlot rows
  playlist_window(...) return PlaylistEntry list covering [start, end) for a
                       channel, reading committed slots (and looping the last
                       known schedule if the user hasn't scheduled that far).
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlmodel import Session, select

from app.models import (MediaItem, Channel, ScheduleSlot, ScheduleRule,
                        MediaType)
from app.core.streamer import PlaylistEntry

UTC = timezone.utc


def _episodes(session: Session, show_title: str) -> List[MediaItem]:
    rows = session.exec(
        select(MediaItem).where(MediaItem.show_title == show_title)).all()
    rows.sort(key=lambda m: (m.season or 0, m.episode or 0, m.title))
    return rows


def _pool(session: Session, media_type: Optional[MediaType]) -> List[MediaItem]:
    stmt = select(MediaItem)
    if media_type:
        stmt = stmt.where(MediaItem.media_type == media_type)
    return session.exec(stmt).all()


def generate_slots(session: Session, channel_id: int, start: datetime,
                   end: datetime, rule: ScheduleRule,
                   show_title: Optional[str] = None,
                   media_type: Optional[MediaType] = None,
                   commercial_between: bool = False) -> List[ScheduleSlot]:
    """
    Fill [start, end) on a channel by laying media end-to-end per `rule`.
    Returns the created (uncommitted) ScheduleSlot rows.

    chronological : sequential episodes of show_title, looping.
    shuffle       : random order of the pool.
    block         : same as chronological/shuffle but caller invokes per block
                    window (recurring handled by the API expanding windows).
    """
    import random

    if show_title:
        pool = _episodes(session, show_title)
    else:
        pool = _pool(session, media_type)
    if not pool:
        return []

    if rule == ScheduleRule.shuffle:
        seed = int(start.timestamp()) ^ channel_id
        random.Random(seed).shuffle(pool)

    commercials = (_pool(session, MediaType.commercial)
                   if commercial_between else [])

    slots: List[ScheduleSlot] = []
    cursor = start
    i = 0
    rng = random.Random(int(start.timestamp()) ^ channel_id ^ 0xC0FFEE)
    while cursor < end:
        item = pool[i % len(pool)]
        i += 1
        dur = timedelta(seconds=item.duration or 0)
        if dur.total_seconds() <= 0:
            continue
        slot_end = min(cursor + dur, end)
        slots.append(ScheduleSlot(
            channel_id=channel_id, media_id=item.id,
            start_time=cursor, end_time=slot_end, rule=rule))
        cursor = slot_end
        if commercials and cursor < end:
            ad = rng.choice(commercials)
            adur = timedelta(seconds=ad.duration or 0)
            if adur.total_seconds() > 0 and cursor < end:
                ad_end = min(cursor + adur, end)
                slots.append(ScheduleSlot(
                    channel_id=channel_id, media_id=ad.id,
                    start_time=cursor, end_time=ad_end,
                    rule=ScheduleRule.block))
                cursor = ad_end
    return slots


def commit_slots(session: Session, slots: List[ScheduleSlot]):
    for s in slots:
        session.add(s)
    session.commit()


def clear_channel_range(session: Session, channel_id: int,
                        start: datetime, end: datetime):
    rows = session.exec(
        select(ScheduleSlot).where(
            ScheduleSlot.channel_id == channel_id,
            ScheduleSlot.start_time >= start,
            ScheduleSlot.start_time < end)).all()
    for r in rows:
        session.delete(r)
    session.commit()


def playlist_window(session: Session, channel_id: int,
                    start: datetime, end: datetime) -> List[PlaylistEntry]:
    """
    Return PlaylistEntry items the streamer should append to cover [start, end).
    Reads committed ScheduleSlots whose start_time falls in the window.

    If the channel has no schedule in the window (user hasn't programmed that
    far), we loop the channel's existing slots so the stream never starves —
    a live channel must always have something to air.
    """
    rows = session.exec(
        select(ScheduleSlot).where(
            ScheduleSlot.channel_id == channel_id,
            ScheduleSlot.end_time > start,
            ScheduleSlot.start_time < end)
        .order_by(ScheduleSlot.start_time)).all()

    entries: List[PlaylistEntry] = []
    for slot in rows:
        media = session.get(MediaItem, slot.media_id)
        if media and media.path:
            entries.append(PlaylistEntry(path=media.path,
                                         duration=media.duration or 0))

    if entries:
        return entries

    # fallback: loop whatever is scheduled on the channel at all
    any_slots = session.exec(
        select(ScheduleSlot).where(ScheduleSlot.channel_id == channel_id)
        .order_by(ScheduleSlot.start_time).limit(200)).all()
    seconds_needed = (end - start).total_seconds()
    filled = 0.0
    i = 0
    media_cache = {}
    while filled < seconds_needed and any_slots:
        slot = any_slots[i % len(any_slots)]
        i += 1
        media = media_cache.get(slot.media_id) or session.get(MediaItem, slot.media_id)
        media_cache[slot.media_id] = media
        if media and media.path and media.duration:
            entries.append(PlaylistEntry(path=media.path, duration=media.duration))
            filled += media.duration
        if i > 10000:
            break
    return entries
