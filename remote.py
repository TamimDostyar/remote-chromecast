#!/usr/bin/env python3



"""

	ping 29731b5f-9712-af76-ecbd-6fb2bf627d6f.local


	dns-sd -L "Chromecast-HD-29731b5f9712af76ecbd6fb2bf627d6f" _googlecast._tcp local




"""
#!/usr/bin/env python3
"""
Chromecast Remote
-----------------
pip install pychromecast androidtvremote2
"""

import asyncio
import json
import pathlib
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

# ─────────────────────────────────────────────
#  Persistent config  (~/.chromecast_remote.json)
# ─────────────────────────────────────────────

CONFIG_PATH = pathlib.Path.home() / ".chromecast_remote.json"

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}

def save_config(data: dict):
    try:
        existing = load_config()
        existing.update(data)
        CONFIG_PATH.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        print(f"[config] save failed: {e}")

try:
    import pychromecast
    from zeroconf import Zeroconf
except ImportError:
    print("Run: pip install pychromecast"); exit(1)

try:
    from androidtvremote2 import AndroidTVRemote
    HAS_NAV = True
except ImportError:
    HAS_NAV = False


# ─────────────────────────────────────────────
#  Media  (pychromecast)
# ─────────────────────────────────────────────

class Media:
    def __init__(self):
        self.cast      = None
        self._zc       = None
        self._browser  = None
        self._host_map = {}   # friendly_name → host IP

    def scan(self):
        """Discover Chromecasts on the LAN. Returns {name: ip}."""
        self._stop_browser()
        self._zc  = Zeroconf()
        found     = {}

        def on_add(uuid, service):
            dev = self._browser.devices.get(uuid)
            if dev:
                found[dev.friendly_name]          = dev.host
                self._host_map[dev.friendly_name] = dev.host

        self._browser = pychromecast.CastBrowser(
            pychromecast.SimpleCastListener(on_add), self._zc)
        self._browser.start_discovery()
        time.sleep(5)
        # FIX: Stop the scan browser NOW so its Zeroconf instance is free
        # before connect() tries to create another one.
        self._stop_browser()
        return found

    def connect(self, name):
        """Connect to a named Chromecast. Returns host IP."""
        # FIX: Always use a fresh Zeroconf for the connect phase so there is
        # no conflict with a still-running scan browser.
        casts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=[name],
            discovery_timeout=10,
        )
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
                muted = bool(self.cast.status.volume_muted)
                self.cast.set_volume_muted(not muted)
            except Exception as e:
                print(f"[mute] {e}")

    def status(self):
        if not self.cast:
            return {}
        try:
            s  = self.cast.status
            ms = self.mc.status if self.mc else None
            return {
                "title":  (ms.title if ms and ms.title else "—"),
                "app":    (self.cast.app_display_name or "—"),
                "state":  (ms.player_state if ms else "—"),
                "volume": round((s.volume_level or 0) * 100),
                "muted":  bool(s.volume_muted),
            }
        except Exception:
            return {}


# ─────────────────────────────────────────────
#  Navigation  (androidtvremote2)
# ─────────────────────────────────────────────

class Nav:
    """
    Single persistent asyncio loop in a background thread.
    Pairing is two-step:
      1. start_pairing(host) → PIN appears on TV → on_pin_ready() called
      2. finish_pairing(pin) → done → on_done(ok, msg) called
    Both steps share the same loop so the remote connection is preserved.
    """
    CERT = "atv_cert.pem"
    KEY  = "atv_key.pem"

    def __init__(self):
        self._remote  = None
        self._pending = None   # AndroidTVRemote waiting for PIN confirmation
        self._loop    = None
        self._lock    = threading.Lock()
        self._start_loop()

    def _start_loop(self):
        self._loop = asyncio.new_event_loop()
        t = threading.Thread(target=self._loop.run_forever, daemon=True)
        t.start()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ── Pairing ──

    def start_pairing(self, host, on_pin_ready, on_done):
        async def _go():
            try:
                r = AndroidTVRemote(
                    client_name="ChromecastRemote",
                    certfile=self.CERT,
                    keyfile=self.KEY,
                    host=host,
                )
                await r.async_generate_cert_if_missing()

                # FIX: async_start_pairing() can return None on some library
                # versions instead of a (name, need_pin) tuple — unpacking None
                # was the "cannot unpack non-iterable NoneType" crash.
                # We treat any non-tuple return as "PIN is required" because
                # the TV already shows the PIN at this point.
                result = await r.async_start_pairing()
                if isinstance(result, tuple):
                    _, need_pin = result
                else:
                    need_pin = True   # safe fallback — PIN box will appear

                print(f"[nav] start_pairing: need_pin={need_pin}")

                if need_pin:
                    with self._lock:
                        self._pending = r
                    on_pin_ready()
                else:
                    with self._lock:
                        self._remote  = r
                        self._pending = None
                    on_done(True, "Connected (no PIN needed)")

            except Exception as e:
                print(f"[nav] start_pairing error: {e}")
                on_done(False, f"Pairing failed: {e}")

        self._run(_go())

    def finish_pairing(self, pin, on_done):
        async def _go():
            with self._lock:
                r = self._pending
            if not r:
                on_done(False, "No active pairing session — press PAIR again")
                return
            try:
                await r.async_finish_pairing(pin)
                with self._lock:
                    self._remote  = r
                    self._pending = None
                on_done(True, "Paired ✓  D-pad & volume ready")
            except Exception as e:
                print(f"[nav] finish_pairing error: {e}")
                on_done(False, f"Wrong PIN or timed out: {e}")

        self._run(_go())

    # ── Connect (after already paired) ──

    def connect(self, host, on_done):
        async def _go():
            try:
                r = AndroidTVRemote(
                    client_name="ChromecastRemote",
                    certfile=self.CERT,
                    keyfile=self.KEY,
                    host=host,
                )
                await r.async_generate_cert_if_missing()
                await r.async_connect()
                with self._lock:
                    self._remote = r
                on_done(True, "Nav connected ✓")
            except Exception as e:
                print(f"[nav] connect error: {e}")
                on_done(False, f"Connect failed: {e}")

        self._run(_go())

    # ── Keys ──

    def key(self, code):
        with self._lock:
            r = self._remote
        if not r:
            return
        try:
            r.send_key_command(code)
        except Exception as e:
            print(f"[nav] key {code}: {e}")

    @property
    def ready(self):
        with self._lock:
            return self._remote is not None


# ─────────────────────────────────────────────
#  Colours
# ─────────────────────────────────────────────

BG      = "#0f1117"
SURFACE = "#1a1f2e"
BORDER  = "#2a2f3e"
ACCENT  = "#4f9eff"
GREEN   = "#3dba6f"
YELLOW  = "#f0a500"
RED     = "#e05555"
TEXT    = "#e8eaf0"
DIM     = "#6b7280"
DPAD    = "#151922"


# ─────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Chromecast Remote")
        self.configure(bg=BG)
        self.resizable(False, False)

        self.media = Media()
        self.nav   = Nav() if HAS_NAV else None

        self._cfg = load_config()

        # FIX: slider debounce — store pending after-id to avoid thread storm
        self._vol_after_id = None

        self._build()
        self._poll_status()

    # ── Helpers ──

    def _btn(self, parent, label, cmd, bg=SURFACE, fg=TEXT, w=8):
        b = tk.Button(parent, text=label, command=cmd,
                      bg=bg, fg=fg, activebackground=ACCENT, activeforeground=BG,
                      relief="flat", bd=0, highlightthickness=0,
                      font=("Menlo", 10, "bold"), cursor="hand2", width=w)
        b.bind("<Enter>", lambda _: b.config(bg=BORDER))
        b.bind("<Leave>", lambda _: b.config(bg=bg))
        return b

    def _section(self, text):
        tk.Label(self, text=text, bg=BG, fg=DIM,
                 font=("Menlo", 8, "bold")).pack(anchor="w", padx=18, pady=(12, 3))

    def _row(self, parent=None):
        f = tk.Frame(parent or self, bg=BG)
        f.pack(padx=18, pady=2)
        return f

    def _thread(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    # ── Build UI ──

    def _build(self):
        # Header
        h = tk.Frame(self, bg=BG)
        h.pack(fill="x", padx=18, pady=(18, 4))
        tk.Label(h, text="CHROMECAST", bg=BG, fg=ACCENT,
                 font=("Menlo", 17, "bold")).pack(side="left")
        tk.Label(h, text=" REMOTE", bg=BG, fg=TEXT,
                 font=("Menlo", 17)).pack(side="left")
        self._dot = tk.Label(h, text="●", bg=BG, fg=RED, font=("Menlo", 14))
        self._dot.pack(side="right")

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=18, pady=4)

        # Device
        self._section("DEVICE")
        dr = tk.Frame(self, bg=BG)
        dr.pack(fill="x", padx=18, pady=(0, 4))

        self._dev_var = tk.StringVar(value=self._cfg.get("last_device", "Press SCAN"))
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("R.TCombobox",
            fieldbackground=SURFACE, background=SURFACE,
            foreground=TEXT, arrowcolor=ACCENT,
            bordercolor=BORDER, selectbackground=SURFACE,
            selectforeground=TEXT)
        style.map("R.TCombobox",
            fieldbackground=[("readonly", SURFACE)],
            foreground=[("readonly", TEXT)])
        self._combo = ttk.Combobox(dr, textvariable=self._dev_var,
            state="readonly", width=18, font=("Menlo", 11), style="R.TCombobox")
        self._combo.pack(side="left", ipady=5)
        self._btn(dr, "SCAN",    self._scan,    SURFACE, ACCENT, w=6).pack(side="left", padx=(6,0), ipady=5)
        self._btn(dr, "CONNECT", self._connect, ACCENT,  BG,     w=8).pack(side="left", padx=(6,0), ipady=5)

        # Now Playing
        self._section("NOW PLAYING")
        card = tk.Frame(self, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=18, pady=(0, 4))
        self._title_lbl = tk.Label(card, text="—", bg=SURFACE, fg=TEXT,
            font=("Menlo", 12, "bold"), wraplength=340, justify="left", anchor="w")
        self._title_lbl.pack(fill="x", padx=12, pady=(10, 2))
        meta = tk.Frame(card, bg=SURFACE)
        meta.pack(fill="x", padx=12, pady=(0, 10))
        self._app_lbl   = tk.Label(meta, text="App: —", bg=SURFACE, fg=DIM, font=("Menlo", 10))
        self._app_lbl.pack(side="left")
        self._state_lbl = tk.Label(meta, text="", bg=SURFACE, fg=DIM, font=("Menlo", 10))
        self._state_lbl.pack(side="left")

        # Volume
        self._section("VOLUME")
        vr = tk.Frame(self, bg=BG)
        vr.pack(fill="x", padx=18, pady=(0, 4))
        self._vol_lbl = tk.Label(vr, text="—", bg=BG, fg=ACCENT,
            font=("Menlo", 12, "bold"), width=5, anchor="w")
        self._vol_lbl.pack(side="left")
        self._slider = tk.Scale(vr, from_=0, to=100, orient="horizontal",
            bg=BG, fg=TEXT, troughcolor=SURFACE, highlightbackground=BG,
            activebackground=ACCENT, showvalue=False, length=200, sliderlength=18,
            command=self._on_slide)
        self._slider.pack(side="left", padx=(4, 8))
        self._btn(vr, "MUTE", self._mute, SURFACE, DIM, w=5).pack(side="left", ipady=4)

        # Playback
        self._section("PLAYBACK")
        pb = self._row()
        self._btn(pb, "◀◀",    self._prev,  w=5).pack(side="left", padx=2, ipady=8)
        self._btn(pb, "▶ PLAY", self._play, GREEN, BG, w=8).pack(side="left", padx=2, ipady=8)
        self._btn(pb, "■",      self._stop,  w=5).pack(side="left", padx=2, ipady=8)
        self._btn(pb, "▶▶",    self._next,  w=5).pack(side="left", padx=2, ipady=8)

        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=18, pady=(8, 4))

        # Nav section
        if not HAS_NAV:
            tk.Label(self, text="pip install androidtvremote2  for D-pad",
                     bg=BG, fg=YELLOW, font=("Menlo", 9)).pack(padx=18, pady=4, anchor="w")
        else:
            self._section("D-PAD  ·  PAIR ONCE TO ENABLE")

            # IP + action buttons row
            nr = tk.Frame(self, bg=BG)
            nr.pack(fill="x", padx=18, pady=(0, 4))
            tk.Label(nr, text="IP:", bg=BG, fg=DIM, font=("Menlo", 10)).pack(side="left")
            # FIX: no hardcoded IP — load from saved config, blank if never set
            self._ip_var = tk.StringVar(value=self._cfg.get("tv_ip", ""))
            tk.Entry(nr, textvariable=self._ip_var, width=14,
                     bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                     relief="flat", font=("Menlo", 11), bd=4).pack(side="left", padx=(6, 0))
            # Save IP to config whenever it changes
            self._ip_var.trace_add("write", lambda *_: save_config({"tv_ip": self._ip_var.get().strip()}))
            self._btn(nr, "PAIR",    self._pair,       YELLOW, BG, w=6).pack(side="left", padx=(6,0), ipady=4)
            self._btn(nr, "CONNECT", self._nav_connect, SURFACE, ACCENT, w=8).pack(side="left", padx=(6,0), ipady=4)

            # Status label
            self._nav_lbl = tk.Label(self,
                text="Enter TV IP  →  press PAIR  →  enter PIN  →  CONNECT",
                bg=BG, fg=DIM, font=("Menlo", 9), wraplength=360, justify="left")
            self._nav_lbl.pack(anchor="w", padx=18, pady=(0, 4))

            # FIX: PIN row always visible — no more hidden frame that never appears.
            # The PIN box is shown from the start so the user can type the code
            # the moment it appears on their TV screen.
            self._pin_frame = tk.Frame(self, bg=BG)
            self._pin_frame.pack(fill="x", padx=18, pady=(0, 6))

            tk.Label(self._pin_frame, text="PIN from TV →", bg=BG, fg=YELLOW,
                     font=("Menlo", 10, "bold")).pack(side="left")
            self._pin_var = tk.StringVar()
            self._pin_entry = tk.Entry(self._pin_frame, textvariable=self._pin_var,
                width=7, font=("Menlo", 20), justify="center",
                bg=SURFACE, fg=YELLOW, insertbackground=YELLOW,
                relief="flat", bd=4)
            self._pin_entry.pack(side="left", padx=(8, 0))
            self._pin_btn = self._btn(self._pin_frame, "SUBMIT", self._submit_pin,
                                      YELLOW, BG, w=7)
            self._pin_btn.pack(side="left", padx=(8, 0), ipady=4)
            self._pin_entry.bind("<Return>", lambda _: self._submit_pin())

            # D-pad
            dp = tk.Frame(self, bg=BG)
            dp.pack(pady=(4, 4))
            self._dbtn(dp, "▲", self._up)
            mid = tk.Frame(dp, bg=BG)
            mid.pack()
            self._dbtn(mid, "◀", self._left,  side="left")
            self._dbtn(mid, "OK", self._ok,   side="left", accent=True)
            self._dbtn(mid, "▶", self._right, side="left")
            self._dbtn(dp, "▼", self._down)
            bot = tk.Frame(dp, bg=BG)
            bot.pack(pady=(4, 0))
            self._btn(bot, "← BACK", self._back, w=8).pack(side="left", padx=4, ipady=6)
            self._btn(bot, "⌂ HOME", self._home, w=8).pack(side="left", padx=4, ipady=6)

            # Vol via nav
            vn = tk.Frame(self, bg=BG)
            vn.pack(pady=(2, 8))
            self._btn(vn, "VOL –", lambda: self.nav.key("VOLUME_DOWN"), SURFACE, DIM,    w=7).pack(side="left", padx=3, ipady=5)
            self._btn(vn, "VOL +", lambda: self.nav.key("VOLUME_UP"),   SURFACE, ACCENT, w=7).pack(side="left", padx=3, ipady=5)
            self._btn(vn, "MUTE",  lambda: self.nav.key("VOLUME_MUTE"), SURFACE, YELLOW, w=7).pack(side="left", padx=3, ipady=5)

        # Keyboard hint
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=18, pady=4)
        tk.Label(self, text="SPACE play/pause  ↑↓ volume  ←→ prev/next  WASD d-pad  B back",
                 bg=BG, fg=DIM, font=("Menlo", 8), justify="center").pack(pady=(2, 14))

        self.bind("<space>",  lambda _: self._play())
        self.bind("<Up>",     lambda _: self._vol_key(+5))
        self.bind("<Down>",   lambda _: self._vol_key(-5))
        self.bind("<Left>",   lambda _: self._prev())
        self.bind("<Right>",  lambda _: self._next())
        self.bind("w",        lambda _: self._up())
        self.bind("a",        lambda _: self._left())
        self.bind("s",        lambda _: self._down())
        self.bind("d",        lambda _: self._right())
        self.bind("b",        lambda _: self._back())

    def _dbtn(self, parent, label, cmd, side="top", accent=False):
        bg = ACCENT if accent else DPAD
        fg = BG     if accent else TEXT
        b = tk.Button(parent, text=label, command=cmd,
                      bg=bg, fg=fg, activebackground=ACCENT, activeforeground=BG,
                      relief="flat", bd=0, highlightthickness=1,
                      highlightbackground=BORDER,
                      font=("Menlo", 11, "bold"), cursor="hand2", width=5, height=2)
        b.bind("<Enter>", lambda _: b.config(bg=BORDER))
        b.bind("<Leave>", lambda _: b.config(bg=bg))
        b.pack(side=side, padx=2, pady=2)

    # ── Media actions ──

    def _scan(self):
        # FIX: only update widgets from the main thread via self.after()
        self.after(0, lambda: self._title_lbl.config(text="Scanning…"))
        self.after(0, lambda: self._dot.config(fg=DIM))

        def go():
            try:
                found = self.media.scan()
                if found:
                    names = list(found.keys())
                    def _apply():
                        self._combo["values"] = names
                        self._dev_var.set(names[0])
                        self._title_lbl.config(text=f"Found: {', '.join(names)}")
                        if HAS_NAV and names[0] in self.media._host_map:
                            self._ip_var.set(self.media._host_map[names[0]])
                        save_config({"last_device": names[0]})
                    self.after(0, _apply)
                else:
                    self.after(0, lambda: self._title_lbl.config(text="No devices found"))
            except Exception as e:
                self.after(0, lambda: self._title_lbl.config(text=f"Scan error: {e}"))

        self._thread(go)

    def _connect(self):
        name = self._dev_var.get()
        if name in ("Press SCAN", ""):
            messagebox.showwarning("", "Press SCAN first")
            return
        self.after(0, lambda: self._title_lbl.config(text=f"Connecting to {name}…"))

        def go():
            try:
                self.media.connect(name)
                save_config({"last_device": name})
                # FIX: widget updates must go through self.after()
                self.after(0, lambda: self._dot.config(fg=GREEN))
                self.after(0, lambda: self._title_lbl.config(text="Connected ✓"))
            except Exception as e:
                self.after(0, lambda: self._dot.config(fg=RED))
                self.after(0, lambda: self._title_lbl.config(text=f"Failed: {e}"))

        self._thread(go)

    def _play(self): self._thread(self.media.play_pause)
    def _stop(self): self._thread(self.media.stop)
    def _next(self): self._thread(self.media.next)
    def _prev(self): self._thread(self.media.prev)

    def _mute(self):
        # FIX: was blocking the main thread with a direct network call
        self._thread(self.media.mute_toggle)

    def _on_slide(self, val):
        # FIX: debounce — cancel any pending volume call and reschedule 150 ms
        # later so rapid slider drags don't spawn hundreds of threads.
        self._vol_lbl.config(text=f"{val}%")
        if self._vol_after_id:
            self.after_cancel(self._vol_after_id)
        self._vol_after_id = self.after(
            150, lambda: self._thread(self.media.set_volume, int(val) / 100))

    def _vol_key(self, delta):
        # FIX: was blocking the main thread with network I/O
        def go():
            if self.media.cast:
                cur = (self.media.cast.status.volume_level or 0) * 100
                self.media.set_volume((cur + delta) / 100)
        self._thread(go)

    # ── Pairing ──

    def _pair(self):
        host = self._ip_var.get().strip()
        if not host:
            messagebox.showwarning("", "Enter the TV IP address")
            return
        self._nav_lbl.config(
            text="Pairing… a PIN will appear on your TV screen", fg=DIM)
        self._pin_var.set("")

        def on_pin_ready():
            # FIX: always route UI changes through self.after()
            self.after(0, lambda: self._nav_lbl.config(
                text="PIN is on your TV  →  type it above and press SUBMIT",
                fg=YELLOW))
            self.after(0, self._pin_entry.focus_set)

        def on_done(ok, msg):
            color = GREEN if ok else RED
            self.after(0, lambda: self._nav_lbl.config(text=msg, fg=color))
            if ok:
                self.after(0, lambda: self._pin_var.set(""))

        self.nav.start_pairing(host, on_pin_ready, on_done)

    def _submit_pin(self):
        pin = self._pin_var.get().strip()
        if not pin:
            self._nav_lbl.config(text="Type the PIN shown on your TV", fg=RED)
            return
        self._nav_lbl.config(text="Sending PIN…", fg=DIM)

        def on_done(ok, msg):
            color = GREEN if ok else RED
            self.after(0, lambda: self._nav_lbl.config(text=msg, fg=color))
            if ok:
                self.after(0, lambda: self._pin_var.set(""))

        self.nav.finish_pairing(pin, on_done)

    def _nav_connect(self):
        host = self._ip_var.get().strip()
        if not host:
            messagebox.showwarning("", "Enter the TV IP address")
            return
        self._nav_lbl.config(text=f"Connecting to {host}…", fg=DIM)

        def on_done(ok, msg):
            color = GREEN if ok else RED
            self.after(0, lambda: self._nav_lbl.config(text=msg, fg=color))

        self.nav.connect(host, on_done)

    # ── D-pad ──

    def _up(self):    self.nav and self.nav.key("DPAD_UP")
    def _down(self):  self.nav and self.nav.key("DPAD_DOWN")
    def _left(self):  self.nav and self.nav.key("DPAD_LEFT")
    def _right(self): self.nav and self.nav.key("DPAD_RIGHT")
    def _ok(self):    self.nav and self.nav.key("DPAD_CENTER")
    def _back(self):  self.nav and self.nav.key("BACK")
    def _home(self):  self.nav and self.nav.key("HOME")

    # ── Status polling ──

    def _poll_status(self):
        def go():
            while True:
                try:
                    if self.media.cast:
                        s = self.media.status()
                        if s:
                            self.after(0, lambda s=s: self._update_status(s))
                except Exception:
                    pass
                time.sleep(3)
        self._thread(go)

    def _update_status(self, s):
        self._title_lbl.config(text=s.get("title", "—"))
        self._app_lbl.config(text=f"App: {s.get('app', '—')}")
        state = s.get("state", "")
        muted = "  🔇" if s.get("muted") else ""
        self._state_lbl.config(text=f"  ·  {state}{muted}")
        vol = s.get("volume", 0)
        self._vol_lbl.config(text=f"{vol}%")
        self._slider.set(vol)


if __name__ == "__main__":
    App().mainloop()
