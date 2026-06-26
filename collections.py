"""
app/services/collections.py — resolve Collections into concrete MediaItems.

A Collection holds ordered CollectionItems, each of which is either:
  - a direct media reference (media_id), or
  - a whole-season reference (show_title + season), which expands to every
    episode of that season in episode order.

resolve_collection() flattens a collection into an ordered MediaItem list the
scheduler can lay onto a timeline.
"""

from typing import List

from sqlmodel import Session, select

from app.models import Collection, CollectionItem, MediaItem


def resolve_collection(session: Session, collection_id: int) -> List[MediaItem]:
    coll = session.get(Collection, collection_id)
    if not coll:
        return []
    items = session.exec(
        select(CollectionItem)
        .where(CollectionItem.collection_id == collection_id)
        .order_by(CollectionItem.order)).all()

    out: List[MediaItem] = []
    for ci in items:
        if ci.media_id:
            m = session.get(MediaItem, ci.media_id)
            if m:
                out.append(m)
        elif ci.show_title and ci.season is not None:
            eps = session.exec(
                select(MediaItem).where(
                    MediaItem.show_title == ci.show_title,
                    MediaItem.season == ci.season)).all()
            eps.sort(key=lambda m: (m.episode or 0, m.title))
            out.extend(eps)
        elif ci.show_title:
            # whole show, all seasons
            eps = session.exec(
                select(MediaItem).where(
                    MediaItem.show_title == ci.show_title)).all()
            eps.sort(key=lambda m: (m.season or 0, m.episode or 0, m.title))
            out.extend(eps)
    return out
