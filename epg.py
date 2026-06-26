"""
app/services/epg.py — XMLTV EPG generator.

Produces a standard XMLTV document covering the next `days` of ScheduleSlots, so
IPTV clients (Plex, Jellyfin, TiViMate) show a proper program guide. Output is a
valid <tv> document with <channel> and <programme> elements; times use the
XMLTV format "YYYYMMDDHHMMSS +0000".
"""

from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from sqlmodel import Session, select

from app.models import Channel, ScheduleSlot, MediaItem

UTC = timezone.utc


def _xmltv_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y%m%d%H%M%S %z")


def _programme_title(media: MediaItem) -> str:
    if media.show_title and media.season and media.episode:
        return f"{media.show_title}"
    return media.title or media.show_title or "Untitled"


def _sub_title(media: MediaItem) -> str:
    if media.show_title and media.episode:
        return media.title or f"S{media.season or 0}E{media.episode}"
    return ""


def generate_epg(session: Session, days: int = 7,
                 base_url: str = "") -> str:
    now = datetime.now(UTC)
    horizon = now + timedelta(days=days)

    channels = session.exec(select(Channel).order_by(Channel.number)).all()

    parts: list[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append('<!DOCTYPE tv SYSTEM "xmltv.dtd">')
    parts.append('<tv generator-info-name="FieldStation">')

    # <channel> elements
    for ch in channels:
        cid = f"fieldstation.{ch.number}"
        parts.append(f'  <channel id="{escape(cid)}">')
        parts.append(f'    <display-name>{escape(ch.name)}</display-name>')
        parts.append(f'    <display-name>{escape(ch.number)}</display-name>')
        if ch.logo_url:
            parts.append(f'    <icon src="{escape(ch.logo_url)}" />')
        parts.append('  </channel>')

    # <programme> elements
    for ch in channels:
        cid = f"fieldstation.{ch.number}"
        slots = session.exec(
            select(ScheduleSlot).where(
                ScheduleSlot.channel_id == ch.id,
                ScheduleSlot.end_time > now,
                ScheduleSlot.start_time < horizon)
            .order_by(ScheduleSlot.start_time)).all()
        for slot in slots:
            media = session.get(MediaItem, slot.media_id)
            if not media:
                continue
            start = _xmltv_time(slot.start_time)
            stop = _xmltv_time(slot.end_time)
            parts.append(
                f'  <programme start="{start}" stop="{stop}" '
                f'channel="{escape(cid)}">')
            parts.append(f'    <title>{escape(_programme_title(media))}</title>')
            sub = _sub_title(media)
            if sub:
                parts.append(f'    <sub-title>{escape(sub)}</sub-title>')
            if media.description:
                parts.append(f'    <desc>{escape(media.description)}</desc>')
            if media.season and media.episode:
                parts.append(
                    f'    <episode-num system="xmltv_ns">'
                    f'{media.season - 1}.{media.episode - 1}.</episode-num>')
            cat = ("Movie" if media.media_type.value == "movie"
                   else "Series")
            parts.append(f'    <category>{cat}</category>')
            parts.append('  </programme>')

    parts.append('</tv>')
    return "\n".join(parts)
