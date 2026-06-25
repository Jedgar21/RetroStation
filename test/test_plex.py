#!/usr/bin/env python3
"""
Tier 4 — Plex source tests.

Two layers:

  MOCK  (always runs) — feeds canned Plex XML through the parser to verify we
        correctly extract sections, movies, and show/season/episode metadata,
        and build direct stream URLs. No network, no real Plex.

  LIVE  (opt-in) — runs only if PLEX_URL and PLEX_TOKEN are set in the env.
        Hits your real server: lists sections, pulls a library, checks items
        have durations + stream URLs. This is the layer I could NOT verify for
        you, so run it against your server before trusting Plex in production:

          PLEX_URL=http://192.168.1.10:32400 PLEX_TOKEN=xxxx \
            python tests/test_plex.py

        Optionally set PLEX_SECTION="TV Shows" to test a specific library.

Run:  python tests/test_plex.py
"""

import os
import sys
import xml.etree.ElementTree as ET
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources import PlexServer, PlexSource


# --------------------------------------------------------------------------- #
# Canned Plex API responses (trimmed to the fields we parse)
# --------------------------------------------------------------------------- #
SECTIONS_XML = """<?xml version="1.0"?>
<MediaContainer>
  <Directory key="1" title="Movies" type="movie"/>
  <Directory key="2" title="TV Shows" type="show"/>
  <Directory key="3" title="Bumpers" type="movie"/>
</MediaContainer>"""

MOVIES_XML = """<?xml version="1.0"?>
<MediaContainer>
  <Video ratingKey="101" title="The Big Movie" duration="5400000">
    <Media><Part key="/library/parts/501/file.mkv" duration="5400000"/></Media>
  </Video>
  <Video ratingKey="102" title="Another Film" duration="6000000">
    <Media><Part key="/library/parts/502/file.mp4" duration="6000000"/></Media>
  </Video>
</MediaContainer>"""

SHOWS_XML = """<?xml version="1.0"?>
<MediaContainer>
  <Directory ratingKey="200" title="Cheers"/>
  <Directory ratingKey="300" title="The News"/>
</MediaContainer>"""

CHEERS_LEAVES_XML = """<?xml version="1.0"?>
<MediaContainer>
  <Video ratingKey="201" title="Give Me a Ring" parentIndex="1" index="1" duration="1320000">
    <Media><Part key="/library/parts/601/s1e1.mkv" duration="1320000"/></Media>
  </Video>
  <Video ratingKey="202" title="Sam at Eleven" parentIndex="1" index="2" duration="1380000">
    <Media><Part key="/library/parts/602/s1e2.mkv" duration="1380000"/></Media>
  </Video>
</MediaContainer>"""

NEWS_LEAVES_XML = """<?xml version="1.0"?>
<MediaContainer>
  <Video ratingKey="301" title="Evening Report" parentIndex="1" index="1" duration="1740000">
    <Media><Part key="/library/parts/701/n1.mkv" duration="1740000"/></Media>
  </Video>
</MediaContainer>"""


def fake_server():
    """A PlexServer whose _get() returns canned XML by path."""
    srv = PlexServer("http://fake:32400", "TESTTOKEN")

    def _get(path, **params):
        if path == "/library/sections":
            return ET.fromstring(SECTIONS_XML)
        if path == "/library/sections/1/all":
            return ET.fromstring(MOVIES_XML)
        if path == "/library/sections/2/all":
            return ET.fromstring(SHOWS_XML)
        if path == "/library/metadata/200/allLeaves":
            return ET.fromstring(CHEERS_LEAVES_XML)
        if path == "/library/metadata/300/allLeaves":
            return ET.fromstring(NEWS_LEAVES_XML)
        raise ValueError(f"unexpected path {path}")

    srv._get = _get
    return srv


# --------------------------------------------------------------------------- #
# MOCK tests (always run)
# --------------------------------------------------------------------------- #
def test_sections_parsed():
    srv = fake_server()
    secs = srv.sections()
    titles = {t for _, t, _ in secs}
    assert {"Movies", "TV Shows", "Bumpers"} <= titles


def test_find_section_by_name_case_insensitive():
    srv = fake_server()
    key, typ = srv.find_section("tv shows")
    assert key == "2" and typ == "show"


def test_part_url_includes_token():
    srv = fake_server()
    url = srv.part_url("/library/parts/501/file.mkv")
    assert url.startswith("http://fake:32400/library/parts/501/file.mkv")
    assert "X-Plex-Token=TESTTOKEN" in url


def test_movie_source_parses_items():
    srv = fake_server()
    items = PlexSource(srv, "Movies").fetch()
    assert len(items) == 2
    m = next(i for i in items if i.title == "The Big Movie")
    assert m.duration == 5400.0                 # ms -> s
    assert m.source_kind == "plex"
    assert m.stream_url().endswith("X-Plex-Token=TESTTOKEN")


def test_show_source_parses_episodes_with_metadata():
    srv = fake_server()
    items = PlexSource(srv, "TV Shows").fetch()
    # 2 Cheers episodes + 1 News episode
    assert len(items) == 3
    cheers = [i for i in items if i.show == "Cheers"]
    assert len(cheers) == 2
    e1 = next(i for i in cheers if i.episode == 1)
    assert e1.season == 1 and e1.episode == 1
    assert e1.duration == 1320.0
    assert "Cheers" in e1.title


def test_show_episodes_sortable_for_grid():
    """Grid scheduling relies on (season, episode) ordering being present."""
    srv = fake_server()
    items = PlexSource(srv, "TV Shows").fetch()
    cheers = sorted((i for i in items if i.show == "Cheers"),
                    key=lambda m: (m.season or 0, m.episode or 0))
    assert [c.episode for c in cheers] == [1, 2]


def test_missing_section_raises():
    srv = fake_server()
    try:
        PlexSource(srv, "Nonexistent").fetch()
        assert False, "expected ValueError"
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# LIVE tests (opt-in via env vars)
# --------------------------------------------------------------------------- #
def _live_enabled():
    return bool(os.environ.get("PLEX_URL") and os.environ.get("PLEX_TOKEN"))


def live_test_connects_and_lists_sections():
    srv = PlexServer(os.environ["PLEX_URL"], os.environ["PLEX_TOKEN"])
    secs = srv.sections()
    assert secs, "no library sections returned (auth or URL problem?)"
    print("    libraries found:", ", ".join(t for _, t, _ in secs))


def live_test_pull_a_library():
    srv = PlexServer(os.environ["PLEX_URL"], os.environ["PLEX_TOKEN"])
    section = os.environ.get("PLEX_SECTION")
    if not section:
        # pick the first movie or show library
        for key, title, typ in srv.sections():
            if typ in ("movie", "show"):
                section = title
                break
    assert section, "no usable library found"
    items = PlexSource(srv, section).fetch()
    assert items, f"library '{section}' returned no playable items"
    sample = items[0]
    assert sample.duration > 0, "item has no duration"
    assert sample.stream_url(), "item has no stream URL"
    print(f"    '{section}': {len(items)} items, "
          f"e.g. {sample.title} ({sample.duration:.0f}s)")


if __name__ == "__main__":
    mock_fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    live_fns = [v for k, v in sorted(globals().items()) if k.startswith("live_test_")]

    passed = 0
    print("MOCK tests:")
    for fn in mock_fns:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); passed += 1
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"  -> {passed}/{len(mock_fns)} mock tests passed")

    print("\nLIVE tests:")
    if not _live_enabled():
        print("  SKIP: set PLEX_URL and PLEX_TOKEN to run against your server")
    else:
        lpassed = 0
        for fn in live_fns:
            try:
                fn(); print(f"  PASS  {fn.__name__}"); lpassed += 1
            except AssertionError as e:
                print(f"  FAIL  {fn.__name__}: {e}")
            except Exception as e:
                print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
        print(f"  -> {lpassed}/{len(live_fns)} live tests passed")

    sys.exit(0 if passed == len(mock_fns) else 1)
