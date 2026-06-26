"""
app/main.py — FastAPI application entrypoint.

Routes:
  GET  /                         drag-and-drop scheduler UI
  GET  /api/channels             list channels
  POST /api/channels             create channel
  GET  /api/profiles             list ffmpeg profiles
  POST /api/profiles             create profile
  GET  /api/media                list media (optionally grouped/filtered)
  POST /api/media/scan           scan a folder into the library
  GET  /api/schedule             slots for a channel + day
  POST /api/schedule/save        save drag-and-drop timeline (JSON)
  POST /api/schedule/generate    expand a rule into slots (chrono/shuffle/block)
  GET  /stream/{number}.ts       continuous MPEG-TS for a channel (IPTV tune-in)
  GET  /epg.xml                  XMLTV guide for the next 7 days
  GET  /playlist.m3u             channel M3U for IPTV players
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request, Body
from fastapi.responses import (StreamingResponse, Response, HTMLResponse,
                               PlainTextResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import engine, init_db, get_session
from app.models import (Channel, FFmpegProfile, MediaItem, ScheduleSlot,
                        ScheduleRule, MediaType, HWAccel)
from app.core.streamer import StreamManager
from app.services import scheduler as sched
from app.services.epg import generate_epg
from app.services.scanner import scan_folder

UTC = timezone.utc
BASE = Path(__file__).resolve().parent

app = FastAPI(title="FieldStation")
templates = Jinja2Templates(directory=str(BASE / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

stream_mgr = StreamManager()


@app.on_event("startup")
def _startup():
    init_db()
    # ensure a default profile exists
    with Session(engine) as s:
        if not s.exec(select(FFmpegProfile)).first():
            s.add(FFmpegProfile(name="Default 720p 16:9"))
            s.commit()


@app.on_event("shutdown")
def _shutdown():
    stream_mgr.stop_all()


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# --------------------------------------------------------------------------- #
# Channels & profiles
# --------------------------------------------------------------------------- #
@app.get("/api/channels")
def list_channels(session: Session = Depends(get_session)):
    chans = session.exec(select(Channel).order_by(Channel.number)).all()
    return [{"id": c.id, "name": c.name, "number": c.number,
             "profile_id": c.profile_id} for c in chans]


@app.post("/api/channels")
def create_channel(body: dict = Body(...),
                   session: Session = Depends(get_session)):
    ch = Channel(name=body["name"], number=str(body["number"]),
                 profile_id=body.get("profile_id"),
                 logo_url=body.get("logo_url"))
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return {"id": ch.id, "name": ch.name, "number": ch.number}


@app.get("/api/profiles")
def list_profiles(session: Session = Depends(get_session)):
    profs = session.exec(select(FFmpegProfile)).all()
    return [p.model_dump() for p in profs]


@app.post("/api/profiles")
def create_profile(body: dict = Body(...),
                   session: Session = Depends(get_session)):
    p = FFmpegProfile(**body)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p.model_dump()


# --------------------------------------------------------------------------- #
# Media library
# --------------------------------------------------------------------------- #
@app.get("/api/media")
def list_media(session: Session = Depends(get_session)):
    items = session.exec(select(MediaItem)).all()
    # group for the sidebar: shows (by show_title), movies, commercials
    shows: dict[str, list] = {}
    movies, commercials = [], []
    for m in items:
        row = {"id": m.id, "title": m.title, "duration": m.duration,
               "season": m.season, "episode": m.episode}
        if m.media_type == MediaType.show and m.show_title:
            shows.setdefault(m.show_title, []).append(row)
        elif m.media_type == MediaType.movie:
            movies.append(row)
        else:
            commercials.append(row)
    for ep in shows.values():
        ep.sort(key=lambda r: (r["season"] or 0, r["episode"] or 0))
    return {"shows": shows, "movies": movies, "commercials": commercials}


@app.post("/api/media/scan")
def scan_media(body: dict = Body(...),
               session: Session = Depends(get_session)):
    folder = body["folder"]
    mtype = body.get("media_type")
    media_type = MediaType(mtype) if mtype else None
    n = scan_folder(session, folder, media_type)
    return {"ok": True, "scanned": n}


# --------------------------------------------------------------------------- #
# Schedule
# --------------------------------------------------------------------------- #
@app.get("/api/schedule")
def get_schedule(channel_id: int, day: str | None = None,
                 session: Session = Depends(get_session)):
    if day:
        d0 = datetime.fromisoformat(day).replace(tzinfo=UTC)
    else:
        d0 = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    d1 = d0 + timedelta(days=1)
    rows = session.exec(
        select(ScheduleSlot).where(
            ScheduleSlot.channel_id == channel_id,
            ScheduleSlot.start_time >= d0,
            ScheduleSlot.start_time < d1)
        .order_by(ScheduleSlot.start_time)).all()
    out = []
    for s in rows:
        m = session.get(MediaItem, s.media_id)
        out.append({
            "id": s.id, "media_id": s.media_id,
            "title": (m.title if m else ""),
            "start": s.start_time.isoformat(),
            "end": s.end_time.isoformat(),
            "rule": s.rule.value})
    return out


@app.post("/api/schedule/save")
def save_schedule(payload: dict = Body(...),
                  session: Session = Depends(get_session)):
    """
    Persist a drag-and-drop timeline. Payload:
      {"channel_id": 2,
       "slots": [{"media_id": 5, "start": ISO, "end": ISO}, ...],
       "replace_range": {"start": ISO, "end": ISO}}   # optional
    """
    channel_id = payload["channel_id"]
    if payload.get("replace_range"):
        rr = payload["replace_range"]
        sched.clear_channel_range(
            session, channel_id,
            datetime.fromisoformat(rr["start"]),
            datetime.fromisoformat(rr["end"]))
    created = 0
    for s in payload.get("slots", []):
        slot = ScheduleSlot(
            channel_id=channel_id, media_id=s["media_id"],
            start_time=datetime.fromisoformat(s["start"]),
            end_time=datetime.fromisoformat(s["end"]),
            rule=ScheduleRule(s.get("rule", "chronological")))
        session.add(slot)
        created += 1
    session.commit()
    return {"ok": True, "created": created}


@app.post("/api/schedule/generate")
def generate_schedule(payload: dict = Body(...),
                      session: Session = Depends(get_session)):
    """
    Expand a rule into slots. Payload:
      {"channel_id":2,"start":ISO,"end":ISO,"rule":"chronological",
       "show_title":"Cheers","media_type":null,"commercial_between":true}
    """
    start = datetime.fromisoformat(payload["start"])
    end = datetime.fromisoformat(payload["end"])
    mt = payload.get("media_type")
    slots = sched.generate_slots(
        session, payload["channel_id"], start, end,
        ScheduleRule(payload.get("rule", "chronological")),
        show_title=payload.get("show_title"),
        media_type=MediaType(mt) if mt else None,
        commercial_between=payload.get("commercial_between", False))
    sched.commit_slots(session, slots)
    return {"ok": True, "created": len(slots)}


# --------------------------------------------------------------------------- #
# Streaming + IPTV integration
# --------------------------------------------------------------------------- #
def _channel_by_number(session: Session, number: str) -> Channel:
    ch = session.exec(select(Channel).where(Channel.number == number)).first()
    if not ch:
        raise HTTPException(404, "channel not found")
    return ch


@app.get("/stream/{number}.ts")
def stream_channel(number: str):
    # resolve channel + profile, then attach to the long-lived ffmpeg process
    with Session(engine) as session:
        ch = _channel_by_number(session, number)
        profile = (session.get(FFmpegProfile, ch.profile_id)
                   if ch.profile_id else None) or FFmpegProfile(name="default")
        channel_id = ch.id

    def fetch_upcoming(frm, to):
        with Session(engine) as s:
            return sched.playlist_window(s, channel_id, frm, to)

    cs = stream_mgr.get_or_start(channel_id, profile, fetch_upcoming)

    def gen():
        try:
            while True:
                chunk = cs.read(65536)
                if not chunk:
                    break
                yield chunk
        except (GeneratorExit, BrokenPipeError):
            pass

    return StreamingResponse(gen(), media_type="video/mp2t")


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
        chans = session.exec(select(Channel).order_by(Channel.number)).all()
        for ch in chans:
            cid = f"fieldstation.{ch.number}"
            logo = f' tvg-logo="{ch.logo_url}"' if ch.logo_url else ""
            lines.append(
                f'#EXTINF:-1 tvg-id="{cid}" tvg-chno="{ch.number}"{logo},'
                f'{ch.name}')
            lines.append(f"{base}/stream/{ch.number}.ts")
    return "\n".join(lines)
