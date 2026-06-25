#!/usr/bin/env python3
"""
RetroStation web server (v2).

Serves the retro TV-guide UI and a live HLS stream per channel, transcoded on
the fly from local files or Plex, seeked to the broadcast offset so you join
programs in progress. Any source codec plays in any modern browser via HLS.

Endpoints:
  GET /                       channel-surfing UI
  GET /api/guide              now-playing + up-next for all channels
  GET /api/now/<num>          what's airing on a channel right now
  GET /hls/<num>/index.m3u8   live HLS playlist (starts ffmpeg if needed)
  GET /hls/<num>/<seg>        HLS segments
"""

import os
import time
import threading
from datetime import datetime

from flask import (Flask, jsonify, send_from_directory, abort,
                   render_template_string, Response)

from station import Station
from transcoder import Transcoder

app = Flask(__name__)
station = Station()
trans = Transcoder()

# debounce restarts: only re-evaluate a channel's session every N secs
_last_eval: dict = {}
_eval_lock = threading.Lock()


def boot():
    if os.path.exists("station.json"):
        station.load()


@app.route("/api/guide")
def api_guide():
    return jsonify(station.guide(datetime.now()))


@app.route("/api/now/<int:num>")
def api_now(num):
    ch = station.channels.get(num)
    if not ch:
        abort(404)
    np = ch.now_playing()
    np["up_next"] = ch.upcoming(count=5)
    return jsonify(np)


def _ensure_stream(num) -> bool:
    """Make sure ffmpeg is producing HLS for whatever is airing now."""
    ch = station.channels.get(num)
    if not ch:
        return False
    np = ch.now_playing()
    if np.get("status") != "on_air" or not np.get("stream_url"):
        return False
    src = np["stream_url"]
    trans.ensure(num, src, np["seek"], np["title"])
    return trans.playlist_ready(num, timeout=15)


@app.route("/hls/<int:num>/index.m3u8")
def hls_playlist(num):
    # throttle how often we re-check the airing item for this channel
    now = time.time()
    with _eval_lock:
        last = _last_eval.get(num, 0)
        due = now - last > 3
        if due:
            _last_eval[num] = now
    if due and not _ensure_stream(num):
        abort(503)
    ch_dir = os.path.join(trans.root, f"ch{num}")
    pl = os.path.join(ch_dir, "index.m3u8")
    if not os.path.exists(pl):
        if not _ensure_stream(num):
            abort(503)
    return send_from_directory(ch_dir, "index.m3u8",
                               mimetype="application/vnd.apple.mpegurl")


@app.route("/hls/<int:num>/<path:seg>")
def hls_segment(num, seg):
    ch_dir = os.path.join(trans.root, f"ch{num}")
    if not os.path.exists(os.path.join(ch_dir, seg)):
        abort(404)
    return send_from_directory(ch_dir, seg, mimetype="video/mp2t")


@app.route("/")
def index():
    return render_template_string(UI)


UI = r"""
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RetroStation</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"></script>
<style>
  :root{--bg:#0a0a0f;--panel:#15151f;--accent:#3ddc84;--dim:#6b6b80;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:#e8e8f0;font-family:'Courier New',monospace}
  .tv{max-width:1100px;margin:0 auto;padding:18px}
  h1{color:var(--accent);letter-spacing:3px;font-size:20px}
  .sub{color:var(--dim);font-size:11px;margin-bottom:16px}
  .screen{background:#000;border:6px solid #222;border-radius:12px;position:relative;
    overflow:hidden;aspect-ratio:16/9;margin-bottom:14px;box-shadow:0 0 40px rgba(61,220,132,.15)}
  .screen video{width:100%;height:100%;background:#000;display:block}
  .scanlines{position:absolute;inset:0;pointer-events:none;
    background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.18) 3px);
    mix-blend-mode:multiply}
  .osd{position:absolute;top:10px;left:12px;background:rgba(0,0,0,.6);color:var(--accent);
    padding:4px 10px;font-size:13px;border-radius:4px}
  .noise{position:absolute;inset:0;display:none;align-items:center;justify-content:center;
    color:var(--dim);font-size:14px;background:#000}
  .guide{display:grid;gap:8px}
  .row{display:flex;align-items:center;gap:12px;background:var(--panel);
    border-left:3px solid transparent;padding:10px 14px;border-radius:6px;cursor:pointer;transition:.15s}
  .row:hover,.row.active{background:#1d1d2c;border-left-color:var(--accent)}
  .num{color:var(--accent);font-weight:bold;width:42px;font-size:18px}
  .meta{flex:1;min-width:0}
  .ch-name{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:1px}
  .ch-title{font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .bar{height:4px;background:#2a2a3a;border-radius:2px;margin-top:5px;overflow:hidden}
  .bar>i{display:block;height:100%;background:var(--accent)}
  .next{color:var(--dim);font-size:11px;text-align:right;min-width:130px}
  .badge{font-size:9px;padding:1px 5px;border-radius:3px;margin-left:6px}
  .badge.ad{background:#c0392b}.badge.live{background:var(--accent);color:#000}
  .badge.plex{background:#e5a00d;color:#000}
</style></head><body>
<div class="tv">
  <h1>&#9626; RETROSTATION</h1>
  <div class="sub">linear broadcast simulator &mdash; local + plex, transcoded live, join mid-program</div>
  <div class="screen">
    <video id="v" controls autoplay playsinline></video>
    <div class="scanlines"></div>
    <div class="osd" id="osd">&mdash; OFF AIR &mdash;</div>
    <div class="noise" id="noise">&#9618;&#9618; NO SIGNAL &#9618;&#9618;</div>
  </div>
  <div class="guide" id="guide"></div>
</div>
<script>
let current=null, hls=null;
async function loadGuide(){
  const rows=await (await fetch('/api/guide')).json();
  const g=document.getElementById('guide'); g.innerHTML='';
  rows.forEach(row=>{
    const pct=row.duration?(1-(row.remaining/row.duration))*100:0;
    const next=(row.up_next&&row.up_next[0])?'NEXT '+row.up_next[0].time+' '+row.up_next[0].title:'';
    let badge=row.kind==='commercial'?'<span class="badge ad">AD</span>':'<span class="badge live">&#9679; LIVE</span>';
    if(row.source_kind==='plex') badge+='<span class="badge plex">PLEX</span>';
    const d=document.createElement('div');
    d.className='row'+(current===row.channel?' active':'');
    d.onclick=()=>tune(row.channel);
    d.innerHTML=`<div class="num">${row.channel}</div><div class="meta">
      <div class="ch-name">${row.name}</div>
      <div class="ch-title">${row.title} ${badge}</div>
      <div class="bar"><i style="width:${pct}%"></i></div></div>
      <div class="next">${next}</div>`;
    g.appendChild(d);
  });
}
function tune(num){
  current=num;
  const v=document.getElementById('v'), osd=document.getElementById('osd'), noise=document.getElementById('noise');
  fetch('/api/now/'+num).then(r=>r.json()).then(np=>{
    osd.textContent='CH '+num+'  '+np.title;
    if(np.status!=='on_air'){
      v.style.display='none'; noise.style.display='flex';
      noise.textContent=np.status==='sign_off'?'&#9618; COLOR BARS &#9618;':'&#9618;&#9618; NO SIGNAL &#9618;&#9618;';
      return;
    }
    noise.style.display='none'; v.style.display='block';
    const url='/hls/'+num+'/index.m3u8';
    if(hls){hls.destroy(); hls=null;}
    if(v.canPlayType('application/vnd.apple.mpegurl')){ v.src=url; v.play(); }
    else if(window.Hls&&Hls.isSupported()){
      hls=new Hls({liveSyncDuration:6}); hls.loadSource(url); hls.attachMedia(v);
      hls.on(Hls.Events.MANIFEST_PARSED,()=>v.play());
    }
  });
  loadGuide();
}
loadGuide(); setInterval(loadGuide,15000);
</script></body></html>
"""

if __name__ == "__main__":
    import atexit
    boot()
    atexit.register(trans.cleanup)
    print("RetroStation v2 on http://localhost:5005")
    try:
        app.run(host="0.0.0.0", port=5005, threaded=True)
    finally:
        trans.cleanup()
