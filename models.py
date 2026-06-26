"""
app/models.py — Database schema (SQLModel).

Entities:
  FFmpegProfile   full transcode profile (video + audio + normalization + hwaccel)
  Channel         a virtual station, with language/subtitle preferences
  MediaItem       a scanned/imported file (show/movie/commercial/music_video/other)
  Collection      a named, ordered set of media (individual files or whole seasons)
  CollectionItem  membership rows for a Collection (file OR a season reference)
  ExternalSource  a linked Plex/Jellyfin server (for category sync + import)
  ScheduleSlot    a concrete airing: this media on this channel, start..end
  ScheduleBlock   a higher-level scheduling directive the user builds in the UI
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
    commercial = "commercial"
    bumper = "bumper"
    music_video = "music_video"
    other = "other"


class HWAccel(str, Enum):
    none = "none"
    nvenc = "nvenc"
    vaapi = "vaapi"
    qsv = "qsv"


class ScheduleRule(str, Enum):
    chronological = "chronological"
    shuffle = "shuffle"
    specific = "specific"          # a chosen episode/file
    block = "block"


class SourceKind(str, Enum):
    local = "local"
    plex = "plex"
    jellyfin = "jellyfin"


class CommercialPlacement(str, Enum):
    none = "none"
    before = "before"
    after = "after"
    middle = "middle"


# --------------------------------------------------------------------------- #
# FFmpegProfile
# --------------------------------------------------------------------------- #
class FFmpegProfile(SQLModel, table=True):
    __tablename__ = "ffmpeg_profile"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)

    # video geometry
    resolution: str = "1280x720"
    aspect_ratio: str = "16:9"           # "16:9" or "4:3"
    fps: int = 30

    # video codec / rate control
    video_codec: str = "libx264"
    video_bitrate: str = "3500k"
    video_bufsize: str = "7000k"
    gop_seconds: int = 2
    video_normalize: bool = False        # normalize luma/levels (loudnorm video eq)

    # audio
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"
    audio_bufsize: str = "256k"
    audio_channels: int = 2
    audio_sample_rate: int = 48000
    audio_normalize: bool = False        # EBU R128 loudnorm

    # hardware acceleration
    hwaccel: HWAccel = Field(default=HWAccel.none)
    hwaccel_device: Optional[str] = "/dev/dri/renderD128"

    channels: List["Channel"] = Relationship(back_populates="profile")


# --------------------------------------------------------------------------- #
# Channel
# --------------------------------------------------------------------------- #
class Channel(SQLModel, table=True):
    __tablename__ = "channel"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    number: str = Field(index=True)
    logo_url: Optional[str] = None

    profile_id: Optional[int] = Field(default=None, foreign_key="ffmpeg_profile.id")
    profile: Optional[FFmpegProfile] = Relationship(back_populates="channels")

    # playback preferences
    preferred_language: Optional[str] = None     # ISO code, e.g. "eng", "jpn"
    burn_subtitles: bool = False                 # burn subs into the video
    subtitle_language: Optional[str] = None      # which sub track to burn

    slots: List["ScheduleSlot"] = Relationship(back_populates="channel")
    blocks: List["ScheduleBlock"] = Relationship(back_populates="channel")


# --------------------------------------------------------------------------- #
# MediaItem
# --------------------------------------------------------------------------- #
class MediaItem(SQLModel, table=True):
    __tablename__ = "media_item"

    id: Optional[int] = Field(default=None, primary_key=True)
    path: str = Field(index=True)
    media_type: MediaType = Field(default=MediaType.show, index=True)
    title: str = ""
    duration: float = 0.0

    show_title: Optional[str] = Field(default=None, index=True)
    season: Optional[int] = None
    episode: Optional[int] = None
    description: Optional[str] = None

    # provenance
    source_kind: SourceKind = Field(default=SourceKind.local)
    source_id: Optional[int] = Field(default=None, foreign_key="external_source.id")
    external_key: Optional[str] = None        # Plex ratingKey / Jellyfin item id
    category: Optional[str] = Field(default=None, index=True)  # library/category name


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #
class Collection(SQLModel, table=True):
    __tablename__ = "collection"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = None

    items: List["CollectionItem"] = Relationship(
        back_populates="collection",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"})


class CollectionItem(SQLModel, table=True):
    __tablename__ = "collection_item"

    id: Optional[int] = Field(default=None, primary_key=True)
    collection_id: int = Field(foreign_key="collection.id", index=True)
    collection: Optional[Collection] = Relationship(back_populates="items")

    order: int = 0

    # either a direct media reference, OR a season reference (show + season)
    media_id: Optional[int] = Field(default=None, foreign_key="media_item.id")
    show_title: Optional[str] = None     # for "whole season" membership
    season: Optional[int] = None         # if set with show_title, pulls the season


# --------------------------------------------------------------------------- #
# External sources (Plex / Jellyfin)
# --------------------------------------------------------------------------- #
class ExternalSource(SQLModel, table=True):
    __tablename__ = "external_source"

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: SourceKind = Field(default=SourceKind.plex)
    name: str = Field(index=True)
    base_url: str
    token: str                            # Plex token or Jellyfin API key
    user_id: Optional[str] = None         # Jellyfin user id (for /Users/{id}/Items)
    last_synced: Optional[datetime] = None


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #
class ScheduleBlock(SQLModel, table=True):
    """
    A high-level scheduling directive built in the UI. Expanded into concrete
    ScheduleSlots by the scheduler. Captures the playback + commercial options
    the user sets when configuring a dropped block.
    """
    __tablename__ = "schedule_block"

    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: int = Field(foreign_key="channel.id", index=True)
    channel: Optional[Channel] = Relationship(back_populates="blocks")

    start_time: datetime = Field(index=True)
    end_time: datetime = Field(index=True)

    # content source: one of these is set
    show_title: Optional[str] = None
    collection_id: Optional[int] = Field(default=None, foreign_key="collection.id")
    media_id: Optional[int] = Field(default=None, foreign_key="media_item.id")
    media_type: Optional[MediaType] = None

    # playback
    rule: ScheduleRule = Field(default=ScheduleRule.chronological)
    specific_media_id: Optional[int] = None     # for rule=specific

    # commercials
    commercial_placement: CommercialPlacement = Field(default=CommercialPlacement.none)
    commercial_collection_id: Optional[int] = Field(
        default=None, foreign_key="collection.id")
    commercial_media_type: Optional[MediaType] = None
    commercial_pad_minutes: int = 0


class ScheduleSlot(SQLModel, table=True):
    __tablename__ = "schedule_slot"

    id: Optional[int] = Field(default=None, primary_key=True)

    channel_id: int = Field(foreign_key="channel.id", index=True)
    channel: Optional[Channel] = Relationship(back_populates="slots")

    media_id: int = Field(foreign_key="media_item.id", index=True)
    start_time: datetime = Field(index=True)
    end_time: datetime = Field(index=True)
    rule: ScheduleRule = Field(default=ScheduleRule.chronological)
    block_id: Optional[int] = Field(default=None, foreign_key="schedule_block.id")
