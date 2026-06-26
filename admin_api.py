#!/usr/bin/env python3
"""
RetroStation admin API.

Registers the management endpoints (everything the GUI config screen needs) onto
the Flask app. Kept separate from server.py so the streaming server stays lean.

All write operations persist to station.json via station.save() and, where
relevant (transcode settings), rebuild the live Transcoder so changes take
effect without a manual restart.

Endpoints (all under /api/admin):
  GET    /state                     full config snapshot for the GUI
  GET    /encoders                  detected encoder availability
  POST   /transcode                 update transcode settings (rebuilds encoder)
  POST   /plex                      add/update a Plex server
  DELETE /plex/<name>               remove a Plex server
  GET    /plex/<name>/sections      list that server's libraries
  POST   /channel                   create or update a channel
  DELETE /channel/<num>             delete a channel
  POST   /channel/<num>/rescan      re-scan a channel's sources
  POST   /channel/<num>/slot        add a grid slot
  DELETE /channel/<num>/slot/<i>    remove slot by index
"""

from flask import jsonify, request, abort

from station import Channel, Slot


def register_admin(app, get_station, get_trans, rebuild_trans):
    """
    Wire admin routes onto `app`.

    get_station() -> Station
    get_trans()   -> current Transcoder (may be None)
    rebuild_trans() -> rebuild the Transcoder from station.transcode settings
    """

    # ----------------------------------------------------------------- state #
    @app.route("/api/admin/state")
    def admin_state():
        st = get_station()
        channels = []
        for num in sorted(st.channels):
            ch = st.channels[num]
            channels.append({
                "number": ch.number, "name": ch.name, "mode": ch.mode,
                "program_sources": ch.program_sources,
                "commercial_sources": ch.commercial_sources,
                "shuffle": ch.shuffle,
                "commercial_break_every": ch.commercial_break_every,
                "commercials_per_break": ch.commercials_per_break,
                "slots": ch.slots,
                "sign_off_start": ch.sign_off_start,
                "sign_off_end": ch.sign_off_end,
                "program_count": len(ch.programs),
                "commercial_count": len(ch.commercials),
            })
        return jsonify({
            "transcode": st.transcode,
            "plex_servers": [
                {"name": n, "base_url": s.base_url}
                for n, s in st.plex_servers.items()
            ],
            "channels": channels,
        })

    # -------------------------------------------------------------- encoders #
    @app.route("/api/admin/encoders")
    def admin_encoders():
        from encoders import (available_ffmpeg_encoders, probe_encoder,
                              resolve_encoder, _AUTO_ORDER)
        compiled = sorted(available_ffmpeg_encoders())
        avail = {}
        for n in _AUTO_ORDER:
            avail[n] = True if n == "cpu" else probe_encoder(n)
        return jsonify({
            "compiled": compiled,
            "available": avail,
            "auto_would_pick": resolve_encoder("auto").name,
        })

    # ------------------------------------------------------------- transcode #
    @app.route("/api/admin/transcode", methods=["POST"])
    def admin_transcode():
        st = get_station()
        body = request.get_json(force=True) or {}
        allowed = {"encoder", "device", "video_bitrate", "preset",
                   "hls_time", "window", "try_copy", "verify_encoder"}
        for k, v in body.items():
            if k in allowed:
                st.transcode[k] = v
        st.save()
        rebuild_trans()
        t = get_trans()
        return jsonify({
            "ok": True,
            "transcode": st.transcode,
            "active_encoder": t.encoder.name if t else None,
            "note": getattr(t.encoder, "fallback_note", None) if t else None,
        })

    # ------------------------------------------------------------------ plex #
    @app.route("/api/admin/plex", methods=["POST"])
    def admin_plex_add():
        st = get_station()
        b = request.get_json(force=True) or {}
        name, url, token = b.get("name"), b.get("base_url"), b.get("token")
        if not (name and url and token):
            return jsonify({"ok": False, "error": "name, base_url, token required"}), 400
        st.add_plex_server(name, url, token)
        st.save()
        return jsonify({"ok": True})

    @app.route("/api/admin/plex/<name>", methods=["DELETE"])
    def admin_plex_del(name):
        st = get_station()
        if name in st.plex_servers:
            del st.plex_servers[name]
            st.save()
        return jsonify({"ok": True})

    @app.route("/api/admin/plex/<name>/sections")
    def admin_plex_sections(name):
        st = get_station()
        srv = st.plex_servers.get(name)
        if not srv:
            abort(404)
        try:
            secs = [{"key": k, "title": t, "type": typ}
                    for k, t, typ in srv.sections()]
            return jsonify({"ok": True, "sections": secs})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 502

    # --------------------------------------------------------------- channel #
    @app.route("/api/admin/channel", methods=["POST"])
    def admin_channel_save():
        """Create or update a channel. Rescans sources."""
        st = get_station()
        b = request.get_json(force=True) or {}
        try:
            num = int(b["number"])
        except (KeyError, ValueError, TypeError):
            return jsonify({"ok": False, "error": "valid number required"}), 400

        ch = Channel(
            number=num,
            name=b.get("name", f"Channel {num}"),
            mode=b.get("mode", "shuffle"),
            program_sources=b.get("program_sources", []),
            commercial_sources=b.get("commercial_sources", []),
            shuffle=b.get("shuffle", True),
            commercial_break_every=float(b.get("commercial_break_every", 0) or 0),
            commercials_per_break=int(b.get("commercials_per_break", 2) or 2),
            slots=b.get("slots", []),
            sign_off_start=b.get("sign_off_start") or None,
            sign_off_end=b.get("sign_off_end") or None,
        )
        try:
            st.add_channel(ch)   # scans sources (may hit Plex/disk)
        except Exception as e:
            return jsonify({"ok": False, "error": f"scan failed: {e}"}), 400
        st.save()
        return jsonify({"ok": True, "program_count": len(ch.programs),
                        "commercial_count": len(ch.commercials)})

    @app.route("/api/admin/channel/<int:num>", methods=["DELETE"])
    def admin_channel_del(num):
        st = get_station()
        if num in st.channels:
            del st.channels[num]
            st.save()
            t = get_trans()
            if t:
                t.stop(num)
        return jsonify({"ok": True})

    @app.route("/api/admin/channel/<int:num>/rescan", methods=["POST"])
    def admin_channel_rescan(num):
        st = get_station()
        ch = st.channels.get(num)
        if not ch:
            abort(404)
        try:
            ch.scan(st.plex_servers)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "program_count": len(ch.programs),
                        "commercial_count": len(ch.commercials)})

    # ------------------------------------------------------------- grid slot #
    @app.route("/api/admin/channel/<int:num>/slot", methods=["POST"])
    def admin_slot_add(num):
        st = get_station()
        ch = st.channels.get(num)
        if not ch:
            abort(404)
        b = request.get_json(force=True) or {}
        days = b.get("days") or []
        if isinstance(days, str):
            days = [d.strip() for d in days.split(",") if d.strip()]
        if not days or not b.get("start"):
            return jsonify({"ok": False, "error": "days and start required"}), 400
        slot = Slot(days=days, start=b["start"],
                    show=b.get("show") or None,
                    block=int(b["block"]) if b.get("block") else None)
        ch.slots.append(slot.__dict__)
        if ch.mode != "grid":
            ch.mode = "grid"
        st.save()
        return jsonify({"ok": True, "slots": ch.slots})

    @app.route("/api/admin/channel/<int:num>/slot/<int:idx>", methods=["DELETE"])
    def admin_slot_del(num, idx):
        st = get_station()
        ch = st.channels.get(num)
        if not ch:
            abort(404)
        if 0 <= idx < len(ch.slots):
            ch.slots.pop(idx)
            st.save()
        return jsonify({"ok": True, "slots": ch.slots})
