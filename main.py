"""
app/main.py — FieldStation FastAPI application.

Pages (HTML):   /  (Channels home)  /ffmpeg  /media  /scheduling
API:            /api/channels, /api/profiles, /api/media, /api/collections,
                /api/sources (Plex/Jellyfin), /api/schedule, /api/blocks
IPTV:           /stream/{number}.ts, /epg.xml, /playlist.m3u, /preview/{number}.ts
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request, Body
from fastapi.responses import (StreamingResponse, Response, HTMLResponse,
                               PlainTextResponse, JSONResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import engine, init_db, get_session
from app.models import (Channel, FFmpegProfile, MediaItem, ScheduleSlot,
                        ScheduleBlock, ScheduleRule, MediaType, HWAccel,
                        Collection, CollectionItem, ExternalSource, SourceKind,
                        CommercialPlacement)
from app.core.streamer import StreamManager
from app.services import scheduler as sched
from app.services.epg import generate_epg
from app.services.scanner import scan_folder
from app.services.collections import resolve_collection
from app.services import external as ext

UTC = timezone.utc
BASE = Path(__file__).resolve().parent

app = FastAPI(title="FieldStation")
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
stream_mgr = StreamManager()


@app.on_event("startup")
def _startup():
    init_db()
    with Session(engine) as s:
        if not s.exec(select(FFmpegProfile)).first():
            s.add(FFmpegProfile(name="Default 720p 16:9"))
            s.commit()


@app.on_event("shutdown")
def _shutdown():
    stream_mgr.stop_all()


# ===================== PAGES ===================== #
def _page(request, name):
    return templates.TemplateResponse(request, name)

@app.get("/", response_class=HTMLResponse)
def page_channels(request: Request): return _page(request, "channels.html")

@app.get("/ffmpeg", response_class=HTMLResponse)
def page_ffmpeg(request: Request): return _page(request, "ffmpeg.html")

@app.get("/media", response_class=HTMLResponse)
def page_media(request: Request): return _page(request, "media.html")

@app.get("/scheduling", response_class=HTMLResponse)
def page_scheduling(request: Request): return _page(request, "scheduling.html")


# ===================== CHANNELS ===================== #
@app.get("/api/channels")
def list_channels(session: Session = Depends(get_session)):
    out = []
    for c in session.exec(select(Channel).order_by(Channel.number)).all():
        out.append({"id": c.id, "name": c.name, "number": c.number,
                    "logo_url": c.logo_url, "profile_id": c.profile_id,
                    "preferred_language": c.preferred_language,
                    "burn_subtitles": c.burn_subtitles,
                    "subtitle_language": c.subtitle_language})
    return out

@app.post("/api/channels")
def create_channel(body: dict = Body(...), session: Session = Depends(get_session)):
    ch = Channel(name=body["name"], number=str(body["number"]),
                 profile_id=body.get("profile_id"), logo_url=body.get("logo_url"),
                 preferred_language=body.get("preferred_language"),
                 burn_subtitles=body.get("burn_subtitles", False),
                 subtitle_language=body.get("subtitle_language"))
    session.add(ch); session.commit(); session.refresh(ch)
    return {"id": ch.id}

@app.put("/api/channels/{cid}")
def update_channel(cid: int, body: dict = Body(...), session: Session = Depends(get_session)):
    ch = session.get(Channel, cid)
    if not ch: raise HTTPException(404)
    for f in ("name", "number", "logo_url", "profile_id", "preferred_language",
              "burn_subtitles", "subtitle_language"):
        if f in body:
            setattr(ch, f, body[f])
    session.add(ch); session.commit()
    return {"ok": True}

@app.delete("/api/channels/{cid}")
def delete_channel(cid: int, session: Session = Depends(get_session)):
    ch = session.get(Channel, cid)
    if ch:
        session.delete(ch); session.commit()
        stream_mgr.stop(cid)
    return {"ok": True}


# ===================== PROFILES ===================== #
@app.get("/api/profiles")
def list_profiles(session: Session = Depends(get_session)):
    return [p.model_dump() for p in session.exec(select(FFmpegProfile)).all()]

@app.post("/api/profiles")
def create_profile(body: dict = Body(...), session: Session = Depends(get_session)):
    p = FFmpegProfile(**body)
    session.add(p); session.commit(); session.refresh(p)
    return p.model_dump()

@app.put("/api/profiles/{pid}")
def update_profile(pid: int, body: dict = Body(...), session: Session = Depends(get_session)):
    p = session.get(FFmpegProfile, pid)
    if not p: raise HTTPException(404)
    for k, v in body.items():
        if hasattr(p, k) and k != "id":
            setattr(p, k, v)
    session.add(p); session.commit()
    return {"ok": True}

@app.delete("/api/profiles/{pid}")
def delete_profile(pid: int, session: Session = Depends(get_session)):
    p = session.get(FFmpegProfile, pid)
    if p:
        session.delete(p); session.commit()
    return {"ok": True}


# ===================== MEDIA ===================== #
@app.get("/api/media")
def list_media(media_type: str = None, category: str = None,
               session: Session = Depends(get_session)):
    stmt = select(MediaItem)
    if media_type:
        stmt = stmt.where(MediaItem.media_type == MediaType(media_type))
    if category:
        stmt = stmt.where(MediaItem.category == category)
    items = session.exec(stmt).all()
    return [_media_dict(m) for m in items]

@app.get("/api/media/grouped")
def media_grouped(session: Session = Depends(get_session)):
    """Group media by type + show, plus the category list from external sources."""
    items = session.exec(select(MediaItem)).all()
    groups = {t.value: {} for t in MediaType}
    categories = set()
    for m in items:
        if m.category:
            categories.add(m.category)
        bucket = groups[m.media_type.value]
        key = m.show_title or "_singles"
        bucket.setdefault(key, []).append(_media_dict(m))
    for t in groups.values():
        for eps in t.values():
            eps.sort(key=lambda r: (r["season"] or 0, r["episode"] or 0, r["title"]))
    return {"groups": groups, "categories": sorted(categories)}

def _media_dict(m):
    return {"id": m.id, "title": m.title, "duration": m.duration,
            "media_type": m.media_type.value, "show_title": m.show_title,
            "season": m.season, "episode": m.episode, "category": m.category,
            "source_kind": m.source_kind.value}

@app.post("/api/media/scan")
def scan_media(body: dict = Body(...), session: Session = Depends(get_session)):
    mt = body.get("media_type")
    n = scan_folder(session, body["folder"], MediaType(mt) if mt else None)
    return {"ok": True, "scanned": n}


# ===================== COLLECTIONS ===================== #
@app.get("/api/collections")
def list_collections(session: Session = Depends(get_session)):
    out = []
    for c in session.exec(select(Collection)).all():
        resolved = resolve_collection(session, c.id)
        out.append({"id": c.id, "name": c.name, "description": c.description,
                    "count": len(resolved)})
    return out

@app.post("/api/collections")
def create_collection(body: dict = Body(...), session: Session = Depends(get_session)):
    c = Collection(name=body["name"], description=body.get("description"))
    session.add(c); session.commit(); session.refresh(c)
    for i, it in enumerate(body.get("items", [])):
        session.add(CollectionItem(
            collection_id=c.id, order=i, media_id=it.get("media_id"),
            show_title=it.get("show_title"), season=it.get("season")))
    session.commit()
    return {"id": c.id}

@app.get("/api/collections/{cid}")
def get_collection(cid: int, session: Session = Depends(get_session)):
    c = session.get(Collection, cid)
    if not c: raise HTTPException(404)
    return {"id": c.id, "name": c.name,
            "items": [_media_dict(m) for m in resolve_collection(session, cid)]}

@app.delete("/api/collections/{cid}")
def delete_collection(cid: int, session: Session = Depends(get_session)):
    c = session.get(Collection, cid)
    if c:
        session.delete(c); session.commit()
    return {"ok": True}


# ===================== EXTERNAL SOURCES ===================== #
@app.get("/api/sources")
def list_sources(session: Session = Depends(get_session)):
    return [{"id": s.id, "kind": s.kind.value, "name": s.name,
             "base_url": s.base_url,
             "last_synced": s.last_synced.isoformat() if s.last_synced else None}
            for s in session.exec(select(ExternalSource)).all()]

@app.post("/api/sources")
def add_source(body: dict = Body(...), session: Session = Depends(get_session)):
    src = ExternalSource(kind=SourceKind(body["kind"]), name=body["name"],
                         base_url=body["base_url"], token=body["token"],
                         user_id=body.get("user_id"))
    session.add(src); session.commit(); session.refresh(src)
    return {"id": src.id}

@app.delete("/api/sources/{sid}")
def del_source(sid: int, session: Session = Depends(get_session)):
    s = session.get(ExternalSource, sid)
    if s:
        session.delete(s); session.commit()
    return {"ok": True}

@app.get("/api/sources/{sid}/categories")
def source_categories(sid: int, session: Session = Depends(get_session)):
    s = session.get(ExternalSource, sid)
    if not s: raise HTTPException(404)
    try:
        return {"ok": True, "categories": ext.list_categories(s)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

@app.post("/api/sources/{sid}/sync")
def sync_source(sid: int, body: dict = Body(default={}),
                session: Session = Depends(get_session)):
    s = session.get(ExternalSource, sid)
    if not s: raise HTTPException(404)
    try:
        n = ext.sync_source(session, s, body.get("categories"))
        return {"ok": True, "imported": n}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


# ===================== SCHEDULE / BLOCKS ===================== #
@app.get("/api/schedule")
def get_schedule(channel_id: int, day: str = None, session: Session = Depends(get_session)):
    if day:
        d0 = datetime.fromisoformat(day).replace(tzinfo=UTC)
    else:
        d0 = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    d1 = d0 + timedelta(days=1)
    rows = session.exec(select(ScheduleSlot).where(
        ScheduleSlot.channel_id == channel_id,
        ScheduleSlot.start_time >= d0, ScheduleSlot.start_time < d1)
        .order_by(ScheduleSlot.start_time)).all()
    out = []
    for s in rows:
        m = session.get(MediaItem, s.media_id)
        out.append({"id": s.id, "title": m.title if m else "",
                    "start": s.start_time.isoformat(), "end": s.end_time.isoformat()})
    return out

@app.post("/api/blocks")
def create_block(body: dict = Body(...), session: Session = Depends(get_session)):
    """Create a scheduling block and immediately expand it into slots."""
    blk = ScheduleBlock(
        channel_id=body["channel_id"],
        start_time=datetime.fromisoformat(body["start"]),
        end_time=datetime.fromisoformat(body["end"]),
        show_title=body.get("show_title"),
        collection_id=body.get("collection_id"),
        media_id=body.get("media_id"),
        media_type=MediaType(body["media_type"]) if body.get("media_type") else None,
        rule=ScheduleRule(body.get("rule", "chronological")),
        specific_media_id=body.get("specific_media_id"),
        commercial_placement=CommercialPlacement(body.get("commercial_placement", "none")),
        commercial_collection_id=body.get("commercial_collection_id"),
        commercial_media_type=MediaType(body["commercial_media_type"]) if body.get("commercial_media_type") else None,
        commercial_pad_minutes=int(body.get("commercial_pad_minutes", 0)))
    session.add(blk); session.commit(); session.refresh(blk)
    # clear overlapping slots then expand
    sched.clear_channel_range(session, blk.channel_id, blk.start_time, blk.end_time)
    slots = sched.expand_block(session, blk)
    sched.commit_slots(session, slots)
    return {"ok": True, "block_id": blk.id, "slots": len(slots)}


# ===================== STREAMING / IPTV ===================== #
def _channel_by_number(session, number):
    ch = session.exec(select(Channel).where(Channel.number == number)).first()
    if not ch: raise HTTPException(404, "channel not found")
    return ch

def _make_stream(channel_id, profile, channel):
    def fetch_upcoming(frm, to):
        with Session(engine) as s:
            return sched.playlist_window(s, channel_id, frm, to)
    return stream_mgr.get_or_start(channel_id, profile, fetch_upcoming, channel=channel)

@app.get("/stream/{number}.ts")
def stream_channel(number: str):
    with Session(engine) as session:
        ch = _channel_by_number(session, number)
        profile = (session.get(FFmpegProfile, ch.profile_id) if ch.profile_id
                   else None) or FFmpegProfile(name="default")
        cid, chan = ch.id, ch
    cs = _make_stream(cid, profile, chan)
    def gen():
        try:
            while True:
                chunk = cs.read(65536)
                if not chunk: break
                yield chunk
        except (GeneratorExit, BrokenPipeError):
            pass
    return StreamingResponse(gen(), media_type="video/mp2t")

# preview is the same stream; the UI plays it with mpegts.js
@app.get("/preview/{number}.ts")
def preview_channel(number: str):
    return stream_channel(number)

@app.get("/epg.xml")
def epg_xml():
    with Session(engine) as session:
        xml = generate_epg(session, days=7)
    return Response(content=xml, media_type="application/xml")

@app.get("/playlist.m3u", response_class=PlainTextResponse)
def playlist_m3u(request: Request):
    base = str(request.base_url).rstrip("/")
    lines = ["#EXTM3U"]
    with Session(engine) as session:
        for ch in session.exec(select(Channel).order_by(Channel.number)).all():
            cid = f"fieldstation.{ch.number}"
            logo = f' tvg-logo="{ch.logo_url}"' if ch.logo_url else ""
            lines.append(f'#EXTINF:-1 tvg-id="{cid}" tvg-chno="{ch.number}"{logo},{ch.name}')
            lines.append(f"{base}/stream/{ch.number}.ts")
    return "\n".join(lines)
