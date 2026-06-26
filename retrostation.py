#!/usr/bin/env python3
"""
RetroStation CLI — manage Plex servers, channels, and the schedule grid.

Examples:

  # connect a Plex server
  python retrostation.py plex-add --name home \
      --url http://192.168.1.10:32400 --token YOURPLEXTOKEN

  # list Plex library sections (to get exact names)
  python retrostation.py plex-sections --name home

  # a shuffle channel from a LOCAL folder, with ads every 20 min
  python retrostation.py channel-add --number 2 --name "TOON TV" --mode shuffle \
      --local ~/media/cartoons --ads-local ~/media/commercials --break-every 1200

  # a shuffle channel sourced from a PLEX library
  python retrostation.py channel-add --number 4 --name "MOVIE NIGHT" --mode shuffle \
      --plex home:Movies

  # a GRID channel sourced from Plex TV shows
  python retrostation.py channel-add --number 5 --name "PRIMETIME" --mode grid \
      --plex home:"TV Shows"
  python retrostation.py slot-add --channel 5 --days mon,tue,wed,thu,fri \
      --start 18:00 --show "The News" --block 30
  python retrostation.py slot-add --channel 5 --days mon,tue,wed,thu,fri \
      --start 18:30 --show "Cheers" --block 30

  python retrostation.py guide
  python retrostation.py watch 5        # ffplay, joins in progress
"""

import argparse
import os
import subprocess
from datetime import datetime

from station import Station, Channel, Slot


def load_station() -> Station:
    s = Station()
    if os.path.exists("station.json"):
        s.load()
    return s


def parse_plex(spec: str) -> dict:
    """'home:TV Shows' -> {'type':'plex','server':'home','section':'TV Shows'}"""
    server, section = spec.split(":", 1)
    return {"type": "plex", "server": server, "section": section, "kind": "program"}


def main():
    ap = argparse.ArgumentParser(description="RetroStation")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("plex-add")
    p.add_argument("--name", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--token", required=True)

    p = sub.add_parser("plex-sections")
    p.add_argument("--name", required=True)

    p = sub.add_parser("channel-add")
    p.add_argument("--number", type=int, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--mode", choices=["shuffle", "grid"], default="shuffle")
    p.add_argument("--local", action="append", default=[], help="local program dir (repeatable)")
    p.add_argument("--plex", action="append", default=[], help="server:Section (repeatable)")
    p.add_argument("--ads-local", default="", help="local commercials dir")
    p.add_argument("--ads-plex", default="", help="server:Section for commercials")
    p.add_argument("--break-every", type=float, default=0.0, help="secs between ad breaks")
    p.add_argument("--signoff", default="", help="HH:MM-HH:MM dead-air window")

    p = sub.add_parser("slot-add")
    p.add_argument("--channel", type=int, required=True)
    p.add_argument("--days", required=True, help="comma list e.g. mon,tue,wed")
    p.add_argument("--start", required=True, help="HH:MM")
    p.add_argument("--show", default=None, help="series title (omit = any)")
    p.add_argument("--block", type=int, default=None, help="minutes")

    sub.add_parser("guide")
    sub.add_parser("list")
    sub.add_parser("encoders")     # detect available hardware encoders

    p = sub.add_parser("transcode-set")
    p.add_argument("--encoder", choices=["auto", "cpu", "nvenc", "qsv",
                                          "vaapi", "videotoolbox"],
                   help="which H.264 encoder to use")
    p.add_argument("--device", help="HW device, e.g. /dev/dri/renderD128 (vaapi)")
    p.add_argument("--bitrate", help="video bitrate, e.g. 3500k or 6000k")
    p.add_argument("--preset", help="encoder preset (backend-specific)")
    p.add_argument("--hls-time", type=int, help="HLS segment length in seconds")
    p.add_argument("--no-copy", action="store_true",
                   help="always re-encode (don't remux h264/aac sources)")
    p.add_argument("--no-verify", action="store_true",
                   help="skip the startup probe of the chosen encoder")

    w = sub.add_parser("watch")
    w.add_argument("number", type=int)

    args = ap.parse_args()
    station = load_station()

    if args.cmd == "plex-add":
        station.add_plex_server(args.name, args.url, args.token)
        station.save()
        print(f"Plex server '{args.name}' saved.")

    elif args.cmd == "plex-sections":
        srv = station.plex_servers.get(args.name)
        if not srv:
            print("No such Plex server. Add it with plex-add."); return
        for key, title, typ in srv.sections():
            print(f"  [{typ:5}] {title}")

    elif args.cmd == "channel-add":
        prog_sources = [{"type": "local", "dir": d, "kind": "program"} for d in args.local]
        prog_sources += [parse_plex(s) for s in args.plex]
        ad_sources = []
        if args.ads_local:
            ad_sources.append({"type": "local", "dir": args.ads_local, "kind": "commercial"})
        if args.ads_plex:
            c = parse_plex(args.ads_plex); c["kind"] = "commercial"; ad_sources.append(c)
        so_s = so_e = None
        if args.signoff and "-" in args.signoff:
            so_s, so_e = args.signoff.split("-", 1)
        ch = Channel(number=args.number, name=args.name, mode=args.mode,
                     program_sources=prog_sources, commercial_sources=ad_sources,
                     commercial_break_every=args.break_every,
                     sign_off_start=so_s, sign_off_end=so_e)
        station.add_channel(ch)
        station.save()
        print(f"CH{args.number} '{args.name}' [{args.mode}] added: "
              f"{len(ch.programs)} programs, {len(ch.commercials)} ads")

    elif args.cmd == "slot-add":
        ch = station.channels.get(args.channel)
        if not ch:
            print("No such channel."); return
        slot = Slot(days=[d.strip() for d in args.days.split(",")],
                    start=args.start, show=args.show, block=args.block)
        ch.slots.append(slot.__dict__)
        if ch.mode != "grid":
            ch.mode = "grid"
            print("(switched channel to grid mode)")
        station.save()
        print(f"Slot added to CH{args.channel}: {args.days} {args.start} "
              f"{args.show or 'ANY'} ({args.block or 'natural'}m)")

    elif args.cmd == "encoders":
        from encoders import (available_ffmpeg_encoders, probe_encoder,
                              resolve_encoder, _AUTO_ORDER)
        compiled = available_ffmpeg_encoders()
        print("Compiled H.264 encoders in ffmpeg:",
              ", ".join(sorted(compiled)) or "(none found)")
        print("\nRuntime availability (actually tested on this machine):")
        for n in _AUTO_ORDER:
            if n == "cpu":
                print(f"  cpu          always available (software)")
                continue
            ok = probe_encoder(n)
            print(f"  {n:13}{'WORKS' if ok else 'not available'}")
        picked = resolve_encoder("auto")
        print(f"\n'auto' would select: {picked.name}")

    elif args.cmd == "transcode-set":
        t = station.transcode
        if args.encoder:
            t["encoder"] = args.encoder
        if args.device is not None:
            t["device"] = args.device
        if args.bitrate:
            t["video_bitrate"] = args.bitrate
        if args.preset:
            t["preset"] = args.preset
        if args.hls_time:
            t["hls_time"] = args.hls_time
        if args.no_copy:
            t["try_copy"] = False
        if args.no_verify:
            t["verify_encoder"] = False
        station.save()
        print("Transcode settings:")
        for k, v in t.items():
            print(f"  {k:16} {v}")
        print("\n(restart the server for changes to take effect)")

    elif args.cmd == "list":
        for num in sorted(station.channels):
            ch = station.channels[num]
            print(f"CH{num} {ch.name} [{ch.mode}] "
                  f"{len(ch.programs)} progs, {len(ch.slots)} slots")

    elif args.cmd == "guide":
        for row in station.guide():
            line = f"CH{row['channel']:>3} {row['name']:<16} | {row['title']}"
            if row.get("source_kind") == "plex":
                line += " [plex]"
            if row["status"] == "on_air":
                line += f"  ({row['remaining']:.0f}s left)"
            print(line)
            for u in row.get("up_next", []):
                print(f"          {u['time']}  {u['title']}")

    elif args.cmd == "watch":
        ch = station.channels.get(args.number)
        if not ch:
            print("No such channel."); return
        np = ch.now_playing()
        url = np.get("stream_url")
        if not url:
            print(f"[CH{args.number}] {np['title']} (nothing to play)"); return
        print(f"[CH{args.number}] {np['title']} joining at {np['seek']:.0f}s")
        subprocess.run(["ffplay", "-autoexit", "-ss", str(np["seek"]), url])

    else:
        ap.print_help()


if __name__ == "__main__":
    main()
