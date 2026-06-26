"""
app/models.py — Database schema (SQLModel = SQLAlchemy core + Pydantic).

Four entities:
  FFmpegProfile  encoder/resolution/aspect settings, referenced by Channels
  Channel        a virtual station (number like "2.1") bound to one profile
  MediaItem      a scanned file (show/movie/commercial) with duration + metadata
  ScheduleSlot   "this MediaItem plays on this Channel from start_time..end_time"

Design notes:
  - Times are stored as timezone-aware UTC datetimes. The scheduler and EPG
    convert for display; the streamer compares against datetime.now(tz=utc).
  - MediaItem.duration is seconds (float) so we can compute slot end times and
    EPG stop offsets exactly.
  - A ScheduleSlot references a MediaItem by id; deleting media is guarded so we
    don't orphan the schedule (handled in the service layer).
"""

from datetime import datetime
from enum import Enum
from typing import Optional, List

from sqlmodel import SQLModel, Field, Relationship


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class MediaType(str, Enum):
    show = "show"
    movie = "movie"
    commercial = "commercial"   # also covers bumpers/idents
    bumper = "bumper"


class HWAccel(str, Enum):
    none = "none"
    nvenc = "nvenc"          # NVIDIA
    vaapi = "vaapi"          # Intel/AMD on Linux (QuickSync via VAAPI)
    qsv = "qsv"              # Intel QuickSync native


class ScheduleRule(str, Enum):
    chronological = "chronological"   # S1E1, S1E2, ...
    shuffle = "shuffle"               # random order
    block = "block"                   # recurring block (e.g. daily 4pm)


# --------------------------------------------------------------------------- #
# FFmpegProfile
# --------------------------------------------------------------------------- #
class FFmpegProfile(SQLModel, table=True):
    __tablename__ = "ffmpeg_profile"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)

    # output geometry
    resolution: str = "1280x720"        # e.g. 1280x720, 854x480, 640x480
    aspect_ratio: str = "16:9"          # "16:9" or "4:3" for retro content
    fps: int = 30

    # codecs / rate control
    video_codec: str = "libx264"        # overridden per-hwaccel at runtime
    audio_codec: str = "aac"
    video_bitrate: str = "3500k"
    audio_bitrate: str = "128k"
    gop_seconds: int = 2

    # hardware acceleration
    hwaccel: HWAccel = Field(default=HWAccel.none)
    hwaccel_device: Optional[str] = "/dev/dri/renderD128"   # for vaapi/qsv

    channels: List["Channel"] = Relationship(back_populates="profile")


# --------------------------------------------------------------------------- #
# Channel
# --------------------------------------------------------------------------- #
class Channel(SQLModel, table=True):
    __tablename__ = "channel"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    number: str = Field(index=True, description='dial position, e.g. "2.1"')
    logo_url: Optional[str] = None

    profile_id: Optional[int] = Field(default=None, foreign_key="ffmpeg_profile.id")
    profile: Optional[FFmpegProfile] = Relationship(back_populates="channels")

    slots: List["ScheduleSlot"] = Relationship(back_populates="channel")


# --------------------------------------------------------------------------- #
# MediaItem
# --------------------------------------------------------------------------- #
class MediaItem(SQLModel, table=True):
    __tablename__ = "media_item"

    id: Optional[int] = Field(default=None, primary_key=True)
    path: str = Field(index=True)
    media_type: MediaType = Field(default=MediaType.show, index=True)
    title: str = ""
    duration: float = 0.0                # seconds

    # series metadata (null for movies/commercials)
    show_title: Optional[str] = Field(default=None, index=True)
    season: Optional[int] = None
    episode: Optional[int] = None

    description: Optional[str] = None

    slots: List["ScheduleSlot"] = Relationship(back_populates="media")


# --------------------------------------------------------------------------- #
# ScheduleSlot
# --------------------------------------------------------------------------- #
class ScheduleSlot(SQLModel, table=True):
    __tablename__ = "schedule_slot"

    id: Optional[int] = Field(default=None, primary_key=True)

    channel_id: int = Field(foreign_key="channel.id", index=True)
    channel: Optional[Channel] = Relationship(back_populates="slots")

    media_id: int = Field(foreign_key="media_item.id", index=True)
    media: Optional[MediaItem] = Relationship(back_populates="slots")

    start_time: datetime = Field(index=True)   # UTC
    end_time: datetime = Field(index=True)      # UTC

    # how this slot was generated (for regeneration / display)
    rule: ScheduleRule = Field(default=ScheduleRule.chronological)
