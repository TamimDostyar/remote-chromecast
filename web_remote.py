#!/usr/bin/env python3
"""
Chromecast Remote — Web Server
pip install pychromecast androidtvremote2 flask
"""

import asyncio
import json
import pathlib
import threading
import time

from flask import Flask, jsonify, request

PORT = 8080

CONFIG_PATH = pathlib.Path.home() / ".chromecast_remote.json"


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(data: dict):
    try:
        c = load_config()
        c.update(data)
        CONFIG_PATH.write_text(json.dumps(c, indent=2))
    except Exception as e:
        print(f"[config] {e}")
try:
    import pychromecast
    from zeroconf import Zeroconf
except ImportError:
    print("Run: pip install pychromecast")
    raise SystemExit(1)

try:
    from androidtvremote2 import AndroidTVRemote
    HAS_NAV = True
except ImportError:
    HAS_NAV = False


class Media:
    def __init__(self):
        self.cast      = None
        self._zc       = None
        self._browser  = None
        self._host_map = {}

    def scan(self):
        self._stop_browser()
        self._zc = Zeroconf()
        found = {}

        def on_add(uuid, service):
            dev = self._browser.devices.get(uuid)
            if dev:
                found[dev.friendly_name]          = dev.host
                self._host_map[dev.friendly_name] = dev.host

        self._browser = pychromecast.CastBrowser(
            pychromecast.SimpleCastListener(on_add), self._zc)
        self._browser.start_discovery()
        time.sleep(5)
        self._stop_browser()
        return found

    def connect(self, name):
        casts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=[name], discovery_timeout=10)
        if not casts:
            raise RuntimeError(f"'{name}' not found — try SCAN again")
        self.cast = casts[0]
        self.cast.wait(timeout=10)
        browser.stop_discovery()
        return self.cast.host

    def _stop_browser(self):
        try:
            if self._browser:
                self._browser.stop_discovery()
        except Exception:
            pass
        try:
            if self._zc:
                self._zc.close()
        except Exception:
            pass
        self._browser = self._zc = None

    @property
    def mc(self):
        return self.cast.media_controller if self.cast else None

    def play_pause(self):
        if not self.mc:
            return
        self.mc.update_status()
        try:
            if self.mc.status.player_state == "PLAYING":
                self.mc.pause()
            else:
                self.mc.play()
        except Exception as e:
            print(f"[play_pause] {e}")

    def stop(self):
        if self.mc:
            try:
                self.mc.stop()
            except Exception as e:
                print(f"[stop] {e}")

    def next(self):
        if self.mc:
            try:
                self.mc.queue_next()
            except Exception as e:
                print(f"[next] {e}")

    def prev(self):
        if self.mc:
            try:
                self.mc.queue_prev()
            except Exception as e:
                print(f"[prev] {e}")

    def set_volume(self, v):
        if self.cast:
            try:
                self.cast.set_volume(max(0.0, min(1.0, v)))
            except Exception as e:
                print(f"[vol] {e}")

    def mute_toggle(self):
        if self.cast:
            try:
                self.cast.set_volume_muted(not bool(self.cast.status.volume_muted))
            except Exception as e:
                print(f"[mute] {e}")

    def get_status(self):
        if not self.cast:
            return {}
        try:
            s  = self.cast.status
            ms = self.mc.status if self.mc else None
            return {
                "title":  ms.title if ms and ms.title else "—",
                "app":    self.cast.app_display_name or "—",
                "state":  ms.player_state if ms else "—",
                "volume": round((s.volume_level or 0) * 100),
                "muted":  bool(s.volume_muted),
            }
        except Exception:
            return {}

class Nav:
    CERT = str(pathlib.Path.home() / ".chromecast_remote_cert.pem")
    KEY  = str(pathlib.Path.home() / ".chromecast_remote_key.pem")

    def __init__(self):
        self._remote  = None
        self._pending = None
        self._lock    = threading.Lock()
        self._loop    = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def start_pairing(self, host, on_pin_ready, on_done):
        async def _go():
            try:
                r = AndroidTVRemote("ChromecastRemote", self.CERT, self.KEY, host)
                await r.async_generate_cert_if_missing()
                result   = await r.async_start_pairing()
                need_pin = result[1] if isinstance(result, tuple) else True
                if need_pin:
                    with self._lock:
                        self._pending = r
                    on_pin_ready()
                else:
                    with self._lock:
                        self._remote, self._pending = r, None
                    on_done(True, "Connected (no PIN needed)")
            except Exception as e:
                on_done(False, f"Pairing failed: {e}")
        self._run(_go())

    def finish_pairing(self, pin, on_done):
        async def _go():
            with self._lock:
                r = self._pending
            if not r:
                on_done(False, "No pairing session — press PAIR again")
                return
            try:
                await r.async_finish_pairing(pin)
                with self._lock:
                    self._remote, self._pending = r, None
                on_done(True, "Paired ✓  D-pad ready")
            except Exception as e:
                on_done(False, f"Wrong PIN or timed out: {e}")
        self._run(_go())

    def connect(self, host, on_done):
        async def _go():
            try:
                r = AndroidTVRemote("ChromecastRemote", self.CERT, self.KEY, host)
                await r.async_generate_cert_if_missing()
                await r.async_connect()
                with self._lock:
                    self._remote = r
                on_done(True, "Nav connected ✓")
            except Exception as e:
                on_done(False, f"Connect failed: {e}")
        self._run(_go())

    def key(self, code):
        with self._lock:
            r = self._remote
        if r:
            try:
                r.send_key_command(code)
            except Exception as e:
                print(f"[nav] key {code}: {e}")

    @property
    def ready(self):
        with self._lock:
            return self._remote is not None


_st_lock = threading.Lock()
_st = dict(
    scan_status="idle", scan_msg="", scan_devices=[],
    connect_status="idle", connect_msg="",
    nav_msg="Enter TV IP → PAIR → type PIN → CONNECT",
)


def _st_set(**kw):
    with _st_lock:
        _st.update(kw)


def _st_get():
    with _st_lock:
        return dict(_st)

app   = Flask(__name__)
media = Media()
nav   = Nav() if HAS_NAV else None

_saved = load_config()
if _saved.get("devices"):
    _st["scan_devices"] = _saved["devices"]
    _st["scan_status"]  = "done"
    _st["scan_msg"]     = "Restored from saved config"


@app.route("/")
def index():
    return HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/status")
def api_status():
    s   = _st_get()
    ms  = media.get_status()
    cfg = load_config()
    return jsonify({
        **s, **ms,
        "connected":     media.cast is not None,
        "nav_available": HAS_NAV,
        "nav_ready":     nav.ready if nav else False,
        "cfg_tv_ip":     cfg.get("tv_ip", ""),
        "cfg_device":    cfg.get("last_device", ""),
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    with _st_lock:
        if _st["scan_status"] == "scanning":
            return jsonify({"ok": False, "message": "Already scanning"})
        _st["scan_status"] = "scanning"
        _st["scan_msg"]    = "Scanning…"

    def _do():
        try:
            found   = media.scan()
            devices = list(found.keys())
            msg     = f"Found: {', '.join(devices)}" if devices else "No devices found"
            _st_set(scan_status="done", scan_devices=devices, scan_msg=msg)
            if devices:
                save_config({"devices": devices, "last_device": devices[0]})
        except Exception as e:
            _st_set(scan_status="error", scan_msg=str(e))

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"ok": False, "message": "No device name"})
    _st_set(connect_status="connecting", connect_msg=f"Connecting to {name}…")

    def _do():
        try:
            host = media.connect(name)
            _st_set(connect_status="connected", connect_msg="Connected ✓")
            save_config({"last_device": name, "tv_ip": host})
        except Exception as e:
            _st_set(connect_status="error", connect_msg=str(e))

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/play_pause", methods=["POST"])
def api_play_pause():
    threading.Thread(target=media.play_pause, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    threading.Thread(target=media.stop, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/next", methods=["POST"])
def api_next():
    threading.Thread(target=media.next, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/prev", methods=["POST"])
def api_prev():
    threading.Thread(target=media.prev, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/volume", methods=["POST"])
def api_volume():
    level = int((request.json or {}).get("level", 50))
    threading.Thread(target=media.set_volume, args=(level / 100,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/mute", methods=["POST"])
def api_mute():
    threading.Thread(target=media.mute_toggle, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/pair", methods=["POST"])
def api_pair():
    if not nav:
        return jsonify({"ok": False, "message": "androidtvremote2 not installed"})
    host = (request.json or {}).get("host", "").strip()
    if not host:
        return jsonify({"ok": False, "message": "No host provided"})
    save_config({"tv_ip": host})

    ev, res = threading.Event(), {}

    def on_pin_ready():
        res["pin_ready"] = True
        _st_set(nav_msg="PIN is on your TV → type it and press SUBMIT")
        ev.set()

    def on_done(ok, msg):
        res.update(ok=ok, message=msg, pin_ready=False)
        _st_set(nav_msg=msg)
        ev.set()

    nav.start_pairing(host, on_pin_ready, on_done)
    ev.wait(timeout=30)
    return jsonify(res or {"ok": False, "message": "Timeout"})


@app.route("/api/pair/pin", methods=["POST"])
def api_pair_pin():
    if not nav:
        return jsonify({"ok": False, "message": "androidtvremote2 not installed"})
    pin = (request.json or {}).get("pin", "").strip()
    if not pin:
        return jsonify({"ok": False, "message": "No PIN provided"})

    ev, res = threading.Event(), {}

    def on_done(ok, msg):
        res.update(ok=ok, message=msg)
        _st_set(nav_msg=msg)
        ev.set()

    nav.finish_pairing(pin, on_done)
    ev.wait(timeout=30)
    return jsonify(res or {"ok": False, "message": "Timeout"})


@app.route("/api/nav/connect", methods=["POST"])
def api_nav_connect():
    if not nav:
        return jsonify({"ok": False, "message": "androidtvremote2 not installed"})
    host = (request.json or {}).get("host", "").strip()
    if not host:
        return jsonify({"ok": False, "message": "No host provided"})

    ev, res = threading.Event(), {}

    def on_done(ok, msg):
        res.update(ok=ok, message=msg)
        _st_set(nav_msg=msg)
        ev.set()

    nav.connect(host, on_done)
    ev.wait(timeout=30)
    return jsonify(res or {"ok": False, "message": "Timeout"})


@app.route("/api/nav/key", methods=["POST"])
def api_nav_key():
    if not nav:
        return jsonify({"ok": False})
    code = (request.json or {}).get("key", "")
    if code:
        threading.Thread(target=nav.key, args=(code,), daemon=True).start()
    return jsonify({"ok": True})


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Chromecast Remote</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f1117;--surface:#1a1f2e;--border:#2a2f3e;
  --accent:#4f9eff;--green:#3dba6f;--yellow:#f0a500;--red:#e05555;
  --text:#e8eaf0;--dim:#6b7280;--dpad:#151922;
}
body{background:var(--bg);color:var(--text);font-family:'Courier New',Courier,monospace;min-height:100vh}
.app{max-width:440px;margin:0 auto;padding:16px 16px 32px}

/* Header */
.header{display:flex;align-items:center;justify-content:space-between;padding-bottom:14px;border-bottom:1px solid var(--border)}
.logo{font-size:20px;font-weight:bold;letter-spacing:1px}
.logo span{color:var(--accent)}
.dot{font-size:18px;color:var(--red);transition:color .3s}
.dot.ok{color:var(--green)}

/* Sections */
.sec{font-size:10px;font-weight:bold;color:var(--dim);letter-spacing:1.5px;margin:18px 0 8px}

/* Card */
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px}

/* Buttons */
.btn{
  background:var(--surface);color:var(--text);
  border:1px solid var(--border);border-radius:8px;
  padding:11px 14px;font-family:inherit;font-size:13px;font-weight:bold;
  cursor:pointer;transition:background .12s,transform .1s;white-space:nowrap;
  -webkit-tap-highlight-color:transparent;touch-action:manipulation;
  display:inline-flex;align-items:center;justify-content:center;
}
.btn:active{background:var(--border);transform:scale(.94)}
.btn.accent{background:var(--accent);color:#0f1117;border-color:var(--accent)}
.btn.green{background:var(--green);color:#0f1117;border-color:var(--green)}
.btn.yellow{background:var(--yellow);color:#0f1117;border-color:var(--yellow)}
.btn.dim{color:var(--dim)}
.btn:disabled{opacity:.4;cursor:not-allowed;transform:none}

/* Rows / layout */
.row{display:flex;gap:8px;align-items:center}
.row.wrap{flex-wrap:wrap}

/* Inputs */
select,input[type=text],input[type=number]{
  background:var(--bg);color:var(--text);
  border:1px solid var(--border);border-radius:8px;
  padding:10px 12px;font-family:inherit;font-size:14px;
  outline:none;-webkit-appearance:none;appearance:none;
}
select:focus,input:focus{border-color:var(--accent)}
select{cursor:pointer;flex:1;min-width:0}

/* Volume */
.vol-label{font-size:15px;font-weight:bold;color:var(--accent);min-width:46px}
input[type=range]{
  flex:1;-webkit-appearance:none;appearance:none;
  height:4px;border-radius:2px;background:var(--border);outline:none;cursor:pointer;
}
input[type=range]::-webkit-slider-thumb{
  -webkit-appearance:none;width:22px;height:22px;
  border-radius:50%;background:var(--accent);cursor:pointer;
}

/* Playback */
.playback{display:flex;gap:8px}
.playback .btn{flex:1;padding:16px 0;font-size:16px}

/* D-pad */
.dpad{display:flex;flex-direction:column;align-items:center;gap:6px;margin:12px 0 4px}
.dpad-row{display:flex;gap:6px}
.dpad-btn{
  width:76px;height:76px;background:var(--dpad);color:var(--text);
  border:1px solid var(--border);border-radius:10px;
  font-family:inherit;font-size:20px;font-weight:bold;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:background .1s,transform .1s;
  -webkit-tap-highlight-color:transparent;touch-action:manipulation;
}
.dpad-btn:active{background:var(--border);transform:scale(.9)}
.dpad-btn.ok{background:var(--accent);color:#0f1117;border-color:var(--accent)}
.dpad-btn.empty{background:transparent;border-color:transparent;pointer-events:none}

/* Nav action rows */
.nav-row{display:flex;gap:8px;margin-top:8px}
.nav-row .btn{flex:1;padding:13px 0;text-align:center}

/* Status messages */
.msg{font-size:12px;color:var(--dim);margin-top:8px;min-height:18px;line-height:1.4}
.msg.ok{color:var(--green)}
.msg.err{color:var(--red)}
.msg.warn{color:var(--yellow)}
.msg.info{color:var(--accent)}

/* Now Playing */
.np-title{font-size:15px;font-weight:bold;margin-bottom:6px;word-break:break-word}
.np-meta{font-size:12px;color:var(--dim)}

/* Pin row */
.pin-row{display:flex;gap:8px;align-items:center;margin-top:10px}
.pin-row .pin-label{color:var(--yellow);font-size:12px;font-weight:bold;white-space:nowrap}
.pin-row input{max-width:90px;text-align:center;font-size:22px;letter-spacing:4px}

hr.div{border:none;border-top:1px solid var(--border);margin:18px 0}

@media(max-width:380px){
  .dpad-btn{width:68px;height:68px;font-size:18px}
}
</style>
</head>
<body>
<div class="app">

  <!-- Header -->
  <div class="header">
    <div class="logo"><span>CHROMECAST</span> REMOTE</div>
    <span class="dot" id="dot">&#9679;</span>
  </div>

  <!-- DEVICE -->
  <div class="sec">DEVICE</div>
  <div class="row wrap">
    <select id="device-select">
      <option value="">&#8212; press SCAN &#8212;</option>
    </select>
    <button class="btn" id="scan-btn" onclick="scan()">SCAN</button>
    <button class="btn accent" id="connect-btn" onclick="connect()">CONNECT</button>
  </div>
  <div class="msg" id="scan-msg"></div>

  <!-- NOW PLAYING -->
  <div class="sec">NOW PLAYING</div>
  <div class="card">
    <div class="np-title" id="np-title">&#8212;</div>
    <div class="np-meta">
      <span id="np-app">App: &#8212;</span>
      <span id="np-state"></span>
    </div>
  </div>

  <!-- VOLUME -->
  <div class="sec">VOLUME</div>
  <div class="row">
    <span class="vol-label" id="vol-label">&#8212;</span>
    <input type="range" id="vol-slider" min="0" max="100" value="50"
      oninput="onVolInput(this.value)"
      onchange="sendVol(this.value)"
      ontouchstart="volDrag=true" ontouchend="volDrag=false"
      onmousedown="volDrag=true" onmouseup="volDrag=false">
    <button class="btn dim" onclick="postApi('mute')">MUTE</button>
  </div>

  <!-- PLAYBACK -->
  <div class="sec">PLAYBACK</div>
  <div class="playback">
    <button class="btn" onclick="postApi('prev')">&#9664;&#9664;</button>
    <button class="btn green" onclick="postApi('play_pause')">&#9654; PLAY</button>
    <button class="btn" onclick="postApi('stop')">&#9632;</button>
    <button class="btn" onclick="postApi('next')">&#9654;&#9654;</button>
  </div>

  <!-- NAV SECTION -->
  <div id="nav-section" style="display:none">
    <hr class="div">
    <div class="sec">D-PAD &middot; PAIR ONCE TO ENABLE</div>

    <div class="card">
      <div class="row wrap" style="gap:8px">
        <input type="text" id="tv-ip" placeholder="TV IP (e.g. 192.168.1.x)" style="flex:1;min-width:0">
        <button class="btn yellow" id="pair-btn" onclick="pair()">PAIR</button>
        <button class="btn" id="nav-connect-btn" onclick="navConnect()">CONNECT</button>
      </div>
      <div class="pin-row">
        <span class="pin-label">PIN from TV &#8594;</span>
        <input type="text" id="pin-input" placeholder="PIN" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
        <button class="btn yellow" id="pin-btn" onclick="submitPin()">SUBMIT</button>
      </div>
      <div class="msg" id="nav-msg">Enter TV IP &#8594; press PAIR &#8594; type PIN &#8594; CONNECT</div>
    </div>

    <!-- D-pad -->
    <div class="dpad">
      <div class="dpad-row">
        <div class="dpad-btn empty"></div>
        <button class="dpad-btn" onclick="navKey('DPAD_UP')">&#9650;</button>
        <div class="dpad-btn empty"></div>
      </div>
      <div class="dpad-row">
        <button class="dpad-btn" onclick="navKey('DPAD_LEFT')">&#9664;</button>
        <button class="dpad-btn ok" onclick="navKey('DPAD_CENTER')">OK</button>
        <button class="dpad-btn" onclick="navKey('DPAD_RIGHT')">&#9654;</button>
      </div>
      <div class="dpad-row">
        <div class="dpad-btn empty"></div>
        <button class="dpad-btn" onclick="navKey('DPAD_DOWN')">&#9660;</button>
        <div class="dpad-btn empty"></div>
      </div>
    </div>

    <div class="nav-row">
      <button class="btn" onclick="navKey('BACK')" style="font-size:12px">&#8592; BACK</button>
      <button class="btn" onclick="navKey('HOME')" style="font-size:12px">&#8962; HOME</button>
    </div>
    <div class="nav-row" style="margin-top:6px">
      <button class="btn dim" onclick="navKey('VOLUME_DOWN')">VOL &#8722;</button>
      <button class="btn accent" onclick="navKey('VOLUME_UP')">VOL +</button>
      <button class="btn dim" onclick="navKey('VOLUME_MUTE')">MUTE</button>
    </div>
  </div>

</div><!-- .app -->

<script>
var volDrag = false, volTimer = null;
var prevScan = 'idle', prevConn = 'idle', init = false;

function q(id){ return document.getElementById(id) }

function setMsg(id, text, cls){
  var el = q(id);
  if(!el) return;
  el.textContent = text || '';
  el.className = 'msg' + (cls ? ' ' + cls : '');
}

async function postApi(ep, body){
  try{
    var opts = {method:'POST'};
    if(body){ opts.headers={'Content-Type':'application/json'}; opts.body=JSON.stringify(body); }
    var r = await fetch('/api/'+ep, opts);
    return await r.json();
  }catch(e){ console.error(e); return {ok:false}; }
}

function updateDevices(devices, sel){
  var s = q('device-select');
  s.innerHTML = devices.map(function(d){ return '<option value="'+d+'">'+d+'</option>'; }).join('');
  if(sel && devices.indexOf(sel) >= 0) s.value = sel;
}

async function scan(){
  setMsg('scan-msg','Scanning\u2026 (takes ~5 seconds)','info');
  q('scan-btn').disabled = true;
  await postApi('scan');
}

async function connect(){
  var name = q('device-select').value;
  if(!name) return;
  setMsg('scan-msg','Connecting to '+name+'\u2026','info');
  q('connect-btn').disabled = true;
  await postApi('connect',{name:name});
}

function onVolInput(val){
  q('vol-label').textContent = val+'%';
}

function sendVol(val){
  if(volTimer) clearTimeout(volTimer);
  volTimer = setTimeout(function(){ postApi('volume',{level:parseInt(val)}); }, 200);
}

async function pair(){
  var host = q('tv-ip').value.trim();
  if(!host){ alert('Enter the TV IP address'); return; }
  q('pair-btn').disabled = true;
  setMsg('nav-msg','Pairing\u2026 PIN will appear on your TV','info');
  try{
    var r = await postApi('pair',{host:host});
    if(r.pin_ready){
      setMsg('nav-msg','PIN shown on TV \u2014 type it above and press SUBMIT','warn');
      q('pin-input').focus();
    } else if(r.ok){
      setMsg('nav-msg', r.message || 'Connected!','ok');
    } else {
      setMsg('nav-msg', r.message || 'Failed','err');
    }
  } finally { q('pair-btn').disabled = false; }
}

async function submitPin(){
  var pin = q('pin-input').value.trim();
  if(!pin){ setMsg('nav-msg','Enter the PIN shown on your TV','err'); return; }
  q('pin-btn').disabled = true;
  setMsg('nav-msg','Sending PIN\u2026','info');
  try{
    var r = await postApi('pair/pin',{pin:pin});
    setMsg('nav-msg', r.message || (r.ok ? 'Paired!' : 'Failed'), r.ok ? 'ok' : 'err');
    if(r.ok) q('pin-input').value = '';
  } finally { q('pin-btn').disabled = false; }
}

async function navConnect(){
  var host = q('tv-ip').value.trim();
  if(!host){ alert('Enter the TV IP address'); return; }
  q('nav-connect-btn').disabled = true;
  setMsg('nav-msg','Connecting to '+host+'\u2026','info');
  try{
    var r = await postApi('nav/connect',{host:host});
    setMsg('nav-msg', r.message || (r.ok ? 'Connected!' : 'Failed'), r.ok ? 'ok' : 'err');
  } finally { q('nav-connect-btn').disabled = false; }
}

function navKey(code){ postApi('nav/key',{key:code}); }

async function poll(){
  try{
    var s = await (await fetch('/api/status')).json();

    // Nav section
    q('nav-section').style.display = s.nav_available ? '' : 'none';

    // Connection dot
    q('dot').className = 'dot' + (s.connected ? ' ok' : '');

    // Scan status changes
    if(s.scan_status !== prevScan || !init){
      prevScan = s.scan_status;
      if(s.scan_status === 'scanning'){
        setMsg('scan-msg','Scanning\u2026','info');
        q('scan-btn').disabled = true;
      } else {
        q('scan-btn').disabled = false;
        if(s.scan_devices && s.scan_devices.length){
          updateDevices(s.scan_devices, s.cfg_device);
        }
        if(s.scan_status !== 'idle'){
          setMsg('scan-msg', s.scan_msg, s.scan_status === 'error' ? 'err' : 'ok');
        }
      }
    }

    // Connect status changes
    if(s.connect_status !== prevConn || !init){
      prevConn = s.connect_status;
      if(s.connect_status === 'connecting'){
        q('connect-btn').disabled = true;
        setMsg('scan-msg', s.connect_msg || 'Connecting\u2026','info');
      } else {
        q('connect-btn').disabled = false;
        if(s.connect_status === 'connected'){
          setMsg('scan-msg', s.connect_msg || 'Connected \u2713','ok');
        } else if(s.connect_status === 'error'){
          setMsg('scan-msg', s.connect_msg || 'Connection failed','err');
        }
      }
    }

    // Now Playing
    q('np-title').textContent = s.title || '\u2014';
    q('np-app').textContent = 'App: ' + (s.app || '\u2014');
    q('np-state').textContent = s.state ? ('  \u00b7  ' + s.state + (s.muted ? '  [muted]' : '')) : '';

    // Volume (skip while dragging)
    if(!volDrag && s.volume != null){
      q('vol-label').textContent = s.volume + '%';
      q('vol-slider').value = s.volume;
    }

    // Restore TV IP from config on first load
    if(!init && s.cfg_tv_ip){
      var ipEl = q('tv-ip');
      if(ipEl && !ipEl.value) ipEl.value = s.cfg_tv_ip;
    }

    init = true;
  } catch(e){ /* server not yet ready */ }
  setTimeout(poll, 3000);
}

poll();
</script>
</body>
</html>"""

if __name__ == "__main__":
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "localhost"

    print()
    print("  Chromecast Remote")
    print("  " + "─" * 30)
    print(f"  Local:   http://localhost:{PORT}")
    print(f"  Network: http://{local_ip}:{PORT}  ← open this on your phone")
    print()

    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False, debug=False)
