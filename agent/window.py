"""Deckster's own control-panel window (Tkinter).

A self-contained native window so the exe doesn't depend on a browser: it shows the
pairing QR + code, the connect URL, the USB/Wi-Fi + TLS toggles, paired devices, and
start-with-Windows. It talks directly to the in-process `Admin`, so no HTTP/browser.

The layout is a collapsible left nav rail (Task-Manager style: click the ☰ to expand
labels or collapse to icons) that switches the right-hand pane between Connect,
Devices, Settings, and About. Built from a few small canvas-drawn widgets (rounded
pill buttons, toggle switches, a status pill) so it reads like a modern dark app.

Runs on its own UI thread (like the tray). Closing the window hides it to the tray
(the server keeps running); Quit stops everything. A small queue carries tray->window
commands ("show"/"quit") so the tray thread never touches Tk.
"""
from __future__ import annotations

import queue
from pathlib import Path

from .log import get_logger

log = get_logger("window")

# palette -----------------------------------------------------------------------
BG = "#0B1018"        # app background
RAIL = "#111824"      # nav rail
CARD = "#172130"      # raised card
CARD2 = "#223247"     # control fill / active nav
LINE = "#344256"      # borders / off-track
INK = "#F3F5F8"       # primary text
INK2 = "#B9BEC9"      # secondary text
SUB = "#7E8494"       # muted labels
ACCENT = "#6DCBFF"
ACCENT_HOVER = "#91D9FF"
ACCENT_INK = "#052033"  # text on an accent fill
GREEN = "#4FDB86"
AMBER = "#FFB454"
RED = "#F06A6A"

RAIL_MIN = 60         # collapsed (icons only)
RAIL_MAX = 194        # expanded (icons + labels)


def _round(cv, x1, y1, x2, y2, r, **kw):
    """Draw a smooth rounded rectangle on a canvas; returns the polygon id."""
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


class PillButton:
    """A rounded button drawn on its own small canvas so it packs like a widget.

    kinds: 'primary' (accent), 'seg' (segmented; use set_active), 'secondary',
    'danger', 'ghost' (borderless).
    """

    def __init__(self, parent, text, command=None, kind="secondary",
                 width=140, height=38, bg=CARD, radius=12,
                 font=("Segoe UI", 10, "bold")):
        import tkinter as tk

        self.cv = tk.Canvas(parent, width=width, height=height, bg=bg,
                            highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.kind = kind
        self.active = False
        self._hover = False
        self._shape = _round(self.cv, 1, 1, width - 1, height - 1, radius,
                             fill=CARD2, outline="")
        self._text = self.cv.create_text(width / 2, height / 2, text=text,
                                         fill=INK, font=font)
        self.cv.bind("<Enter>", self._on_enter)
        self.cv.bind("<Leave>", self._on_leave)
        self.cv.bind("<Button-1>", self._on_click)
        self._apply()

    def pack(self, **kw):
        self.cv.pack(**kw); return self

    def set_text(self, text):
        self.cv.itemconfig(self._text, text=text)

    def set_active(self, active):
        if active != self.active:
            self.active = active
            self._apply()

    def _colors(self):
        if self.kind == "primary":
            return (ACCENT_HOVER if self._hover else ACCENT), ACCENT_INK
        if self.kind == "seg":
            if self.active:
                return (ACCENT_HOVER if self._hover else ACCENT), ACCENT_INK
            return (LINE if self._hover else CARD2), INK2
        if self.kind == "danger":
            return (LINE if self._hover else CARD2), RED
        if self.kind == "ghost":
            return (CARD if self._hover else self.cv["bg"]), INK2
        return (LINE if self._hover else CARD2), INK  # secondary

    def _apply(self):
        fill, fg = self._colors()
        self.cv.itemconfig(self._shape, fill=fill)
        self.cv.itemconfig(self._text, fill=fg)

    def _on_enter(self, _e): self._hover = True; self._apply()
    def _on_leave(self, _e): self._hover = False; self._apply()
    def _on_click(self, _e):
        if self.command:
            self.command()


class ToggleSwitch:
    """An iOS-style toggle drawn on a canvas. set() reflects state without firing."""
    W = 48
    H = 26

    def __init__(self, parent, command=None, bg=BG):
        import tkinter as tk

        self.cv = tk.Canvas(parent, width=self.W, height=self.H, bg=bg,
                            highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self.on = False
        self._track = _round(self.cv, 1, 1, self.W - 1, self.H - 1,
                             (self.H - 2) / 2, fill=LINE, outline="")
        r = self.H - 8
        self._knob = self.cv.create_oval(4, 4, 4 + r, 4 + r, fill=INK, outline="")
        self.cv.bind("<Button-1>", self._on_click)

    def pack(self, **kw):
        self.cv.pack(**kw); return self

    def set(self, on):
        self.on = bool(on); self._render()

    def _render(self):
        r = self.H - 8
        x = (self.W - 4 - r) if self.on else 4
        self.cv.coords(self._knob, x, 4, x + r, 4 + r)
        self.cv.itemconfig(self._track, fill=(ACCENT if self.on else LINE))
        self.cv.itemconfig(self._knob, fill=(ACCENT_INK if self.on else INK))

    def _on_click(self, _e):
        self.on = not self.on
        self._render()
        if self.command:
            self.command(self.on)


class StatusPill:
    """A rounded pill with a coloured dot + short label (connection status)."""

    def __init__(self, parent, bg=BG, width=190, height=28):
        import tkinter as tk

        self.cv = tk.Canvas(parent, width=width, height=height, bg=bg,
                            highlightthickness=0, bd=0)
        _round(self.cv, 1, 1, width - 1, height - 1, height / 2, fill=CARD, outline="")
        cy = height / 2
        self._dot = self.cv.create_oval(13, cy - 4, 21, cy + 4, fill=GREEN, outline="")
        self._txt = self.cv.create_text(30, cy, text="", anchor="w", fill=INK2,
                                        font=("Segoe UI", 9, "bold"))

    def pack(self, **kw):
        self.cv.pack(**kw); return self

    def set(self, text, color):
        self.cv.itemconfig(self._dot, fill=color)
        self.cv.itemconfig(self._txt, text=text)


class DecksterWindow:
    def __init__(self, admin, stop_event, cmd_queue: "queue.Queue", icon_path: str | None = None,
                 soundboard=None, start_hidden=False):
        import tkinter as tk

        self.admin = admin
        self.stop_event = stop_event
        self.cmd_queue = cmd_queue
        self._qr_img = None
        self._qr_shown = ""
        self.rail_expanded = True
        self.active_section = "connect"
        self.soundboard = soundboard

        self.root = tk.Tk()
        if start_hidden:
            self.root.withdraw()
        self.root.title("Deckster")
        self.root.configure(bg=BG)
        self.root.geometry(f"1000x{min(820, max(640, self.root.winfo_screenheight() - 100))}")
        self.root.minsize(840, 640)
        self._icon_img = None
        if icon_path:
            try:
                from PIL import Image, ImageTk
                self._icon_img = ImageTk.PhotoImage(Image.open(icon_path))
                self.root.iconphoto(True, self._icon_img)
            except Exception:  # noqa: BLE001
                pass
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._hide)   # X -> hide to tray, keep running
        self._pump()
        self._refresh()

    # ---- layout -----------------------------------------------------------
    def _build(self):
        import tkinter as tk

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        # left nav rail --------------------------------------------------
        self.rail = tk.Frame(body, bg=RAIL, width=RAIL_MAX)
        self.rail.pack(side="left", fill="y")
        self.rail.pack_propagate(False)
        tk.Frame(body, bg=LINE, width=1).pack(side="left", fill="y")   # hairline divider

        ham = tk.Frame(self.rail, bg=RAIL)
        ham.pack(fill="x")
        self.ham_btn = tk.Label(ham, text="☰", bg=RAIL, fg=INK, cursor="hand2",
                                font=("Segoe UI Symbol", 15), width=2)
        self.ham_btn.pack(side="left", padx=(20, 0), pady=(16, 12))
        self.ham_btn.bind("<Button-1>", lambda e: self._toggle_rail())

        self._nav = {}
        for key, glyph, label in (("connect", "▦", "Connect"),
                                  ("devices", "☷", "Devices"),
                                  ("routing", "⇄", "Audio routing"),
                                  ("soundboard", "♪", "Soundboard"),
                                  ("settings", "⚙", "Settings"),
                                  ("about", "ⓘ", "About")):
            self._nav[key] = self._make_nav(key, glyph, label)

        # right column: header + content + footer ------------------------
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        header = tk.Frame(right, bg=BG)
        header.pack(fill="x", padx=22, pady=(14, 12))
        heading = tk.Frame(header, bg=BG)
        heading.pack(side="left")
        brand = tk.Frame(heading, bg=BG)
        brand.pack(anchor="w")
        tk.Label(brand, text="DECKSTER", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        self.ver = tk.Label(brand, text="", bg=BG, fg=SUB, font=("Segoe UI", 8))
        self.ver.pack(side="left", padx=(7, 0))
        self.page_title = tk.Label(heading, text="Connect", bg=BG, fg=INK,
                                   font=("Segoe UI Semibold", 18))
        self.page_title.pack(anchor="w", pady=(2, 0))
        self.status = StatusPill(header).pack(side="right", pady=2)

        footer = tk.Frame(right, bg=BG)
        footer.pack(side="bottom", fill="x", padx=22, pady=12)
        PillButton(footer, "Hide to tray", self._hide, kind="ghost",
                   width=140, height=36, bg=BG).pack(side="left")
        PillButton(footer, "Quit", self._quit, kind="danger",
                   width=110, height=36, bg=BG).pack(side="right")

        self.content = tk.Frame(right, bg=BG)
        self.content.pack(fill="both", expand=True, padx=22)

        # sections (built once; shown one at a time)
        self.sections = {
            "connect": self._build_connect(),
            "devices": self._build_devices(),
            "routing": self._build_routing(),
            "soundboard": self._build_soundboard(),
            "settings": self._build_settings(),
            "about": self._build_about(),
        }
        self._show_section("connect")

    # ---- nav rail ----------------------------------------------------------
    def _make_nav(self, key, glyph, label):
        import tkinter as tk
        row = tk.Frame(self.rail, bg=RAIL, cursor="hand2")
        row.pack(fill="x")
        ic = tk.Label(row, text=glyph, bg=RAIL, fg=INK2,
                      font=("Segoe UI Symbol", 15), width=2)
        ic.pack(side="left", padx=(20, 0), pady=11)
        tx = tk.Label(row, text=label, bg=RAIL, fg=INK2,
                      font=("Segoe UI", 10, "bold"), anchor="w")
        tx.pack(side="left", padx=(12, 0))
        for w in (row, ic, tx):
            w.bind("<Button-1>", lambda e, k=key: self._show_section(k))
            w.bind("<Enter>", lambda e, k=key: self._nav_hover(k, True))
            w.bind("<Leave>", lambda e, k=key: self._nav_hover(k, False))
        return {"row": row, "ic": ic, "tx": tx}

    def _nav_hover(self, key, on):
        if key == self.active_section:
            return
        bg = "#151a22" if on else RAIL
        n = self._nav[key]
        n["row"].config(bg=bg); n["ic"].config(bg=bg); n["tx"].config(bg=bg)

    def _toggle_rail(self):
        self.rail_expanded = not self.rail_expanded
        self.rail.config(width=RAIL_MAX if self.rail_expanded else RAIL_MIN)
        for n in self._nav.values():
            if self.rail_expanded:
                n["tx"].pack(side="left", padx=(12, 0))
            else:
                n["tx"].pack_forget()

    def _show_section(self, key):
        for f in self.sections.values():
            f.pack_forget()
        self.sections[key].pack(fill="both", expand=True)
        self.active_section = key
        self.page_title.config(text={"connect": "Connect a phone", "devices": "Paired devices",
                                     "routing": "Audio routing", "soundboard": "Soundboard", "settings": "Settings",
                                     "about": "About Deckster"}.get(key, "Deckster"))
        for k, n in self._nav.items():
            active = k == key
            bg = CARD2 if active else RAIL
            fg = ACCENT if active else INK2
            n["row"].config(bg=bg); n["ic"].config(bg=bg, fg=fg); n["tx"].config(bg=bg, fg=fg)

    # ---- sections ----------------------------------------------------------
    def _build_connect(self):
        import tkinter as tk
        f = tk.Frame(self.content, bg=BG)

        qr_card = tk.Frame(f, bg=CARD, highlightthickness=1,
                           highlightbackground=LINE, highlightcolor=LINE)
        qr_card.pack(fill="x")
        white = tk.Frame(qr_card, bg="#FFFFFF")
        white.pack(pady=(14, 8))
        self.qr_label = tk.Label(white, bg="#FFFFFF", padx=8, pady=8)
        self.qr_label.pack()
        self.code = tk.Label(qr_card, text="— — — — — —",
                             bg=CARD, fg=INK, font=("Consolas", 21, "bold"))
        self.code.pack()
        tk.Label(qr_card, text="Scan the QR on the phone, or enter this code",
                 bg=CARD, fg=SUB, font=("Segoe UI", 9)).pack(pady=(2, 8))
        PillButton(qr_card, "New code", self._new_code, kind="secondary",
                   width=132, height=32, bg=CARD, radius=10).pack(pady=(0, 14))

        self.url = tk.Label(f, text="", bg=BG, fg=ACCENT,
                            font=("Consolas", 10), anchor="w")
        self.url.pack(fill="x", pady=(14, 8))

        mode = tk.Frame(f, bg=BG)
        mode.pack(fill="x")
        self.btn_usb = PillButton(mode, "USB · secure", lambda: self._set_mode("loopback"),
                                  kind="seg", width=200, height=40, bg=BG)
        self.btn_usb.pack(side="left")
        self.btn_wifi = PillButton(mode, "Wi-Fi", lambda: self._set_mode("lan"),
                                   kind="seg", width=200, height=40, bg=BG)
        self.btn_wifi.pack(side="right")

        # firewall warning (Wi-Fi only; packed into `f` on demand)
        self.fw_frame = tk.Frame(f, bg=CARD, highlightthickness=1,
                                 highlightbackground=AMBER, highlightcolor=AMBER)
        tk.Label(self.fw_frame, text="⚠  Wi-Fi is blocked by Windows Firewall",
                 bg=CARD, fg=AMBER, font=("Segoe UI", 9, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(self.fw_frame,
                 text="The phone can't reach this PC over Wi-Fi until you allow it. "
                      "USB works without this.",
                 bg=CARD, fg=INK2, font=("Segoe UI", 8), anchor="w",
                 wraplength=420, justify="left").pack(fill="x", padx=12, pady=(1, 8))
        PillButton(self.fw_frame, "Allow Wi-Fi through the firewall", self._allow_firewall,
                   kind="primary", width=420, height=34, bg=CARD).pack(padx=12, pady=(0, 12))
        self._fw_shown = False
        self._fw_parent = f
        return f

    def _build_devices(self):
        import tkinter as tk
        f = tk.Frame(self.content, bg=BG)
        tk.Label(f, text="PAIRED DEVICES", bg=BG, fg=SUB,
                 font=("Segoe UI", 8, "bold")).pack(fill="x", pady=(4, 6))
        card = tk.Frame(f, bg=CARD, highlightthickness=1,
                        highlightbackground=LINE, highlightcolor=LINE)
        card.pack(fill="both", expand=True)
        self.devices = tk.Listbox(card, bg=CARD, fg=INK2, height=6, borderwidth=0,
                                  highlightthickness=0, selectbackground=CARD2,
                                  activestyle="none", font=("Segoe UI", 10))
        self.devices.pack(side="left", fill="both", expand=True, padx=(10, 6), pady=10)
        self._dev_ids: list[str] = []
        col = tk.Frame(card, bg=CARD)
        col.pack(side="right", fill="y", padx=(0, 10), pady=10)
        PillButton(col, "Revoke", self._revoke, kind="danger",
                   width=108, height=32, bg=CARD, radius=9).pack(pady=(0, 6))
        PillButton(col, "Revoke all", self._revoke_all, kind="danger",
                   width=108, height=32, bg=CARD, radius=9).pack()
        tk.Label(f, text="Revoked devices must scan the QR again to reconnect.",
                 bg=BG, fg=SUB, font=("Segoe UI", 8)).pack(fill="x", pady=(8, 0))
        return f

    def _build_settings(self):
        import tkinter as tk
        f = tk.Frame(self.content, bg=BG)
        card = tk.Frame(f, bg=CARD, highlightthickness=1,
                        highlightbackground=LINE, highlightcolor=LINE)
        card.pack(fill="x", pady=(2, 0))
        self.var_secure = tk.IntVar()
        self.var_autostart = tk.IntVar()
        self.secure_toggle = self._toggle_row(card, "Secure connection (TLS)",
                                               "Pin a self-signed cert on Wi-Fi.",
                                               self._toggle_secure)
        tk.Frame(card, bg=LINE, height=1).pack(fill="x", padx=14)
        self.autostart_toggle = self._toggle_row(card, "Start with Windows",
                                                 "Launch Deckster at login.",
                                                 self._toggle_autostart)
        tk.Label(f, text="More settings (idle-screen style, timeouts) are on the way.",
                 bg=BG, fg=SUB, font=("Segoe UI", 8)).pack(fill="x", pady=(12, 0))
        return f

    def _build_routing(self):
        from .routing_panel import RoutingPanel
        self.routing_panel = RoutingPanel(self.content, self.admin)
        return self.routing_panel

    def _build_soundboard(self):
        """Clip library; device setup has its own guided routing page."""
        import tkinter as tk
        f = tk.Frame(self.content, bg=BG)
        PillButton(f, "Open audio routing", lambda: self._show_section("routing"),
                   width=185, bg=BG).pack(anchor="w", pady=(0, 12))
        tk.Label(f, text="SOUNDBOARD CLIPS", bg=BG, fg=SUB,
                 font=("Segoe UI", 8, "bold")).pack(fill="x", pady=(0, 6))
        card = tk.Frame(f, bg=CARD, highlightthickness=1,
                        highlightbackground=LINE, highlightcolor=LINE)
        card.pack(fill="both", expand=True)
        self.soundboard_clips = tk.Listbox(card, bg=CARD, fg=INK2, height=8, borderwidth=0,
                                            highlightthickness=0, activestyle="none",
                                            font=("Segoe UI", 10))
        self.soundboard_clips.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        self._soundboard_clip_ids: list[str] = []
        actions = tk.Frame(card, bg=CARD)
        actions.pack(fill="x", padx=10, pady=(0, 10))
        PillButton(actions, "Add audio clip", self._import_soundboard_clip, kind="primary",
                   width=150, height=34, bg=CARD, radius=9).pack(side="left")
        PillButton(actions, "Remove selected", self._remove_soundboard_clip, kind="danger",
                   width=142, height=34, bg=CARD, radius=9).pack(side="right")
        defaults = tk.Frame(card, bg=CARD)
        defaults.pack(fill="x", padx=10, pady=(0, 10))
        PillButton(defaults, "Restore 12 starter sounds", self._restore_soundboard_defaults,
                   width=190, height=32, bg=CARD, radius=9).pack(side="left")
        self.soundboard_note = tk.Label(
            f, text="Others = your call/game. Me = your headphones/speakers. Choose each clip’s destinations on the phone.",
            bg=BG, fg=SUB, font=("Segoe UI", 8), justify="left", anchor="w", wraplength=420,
        )
        self.soundboard_note.pack(fill="x", pady=(9, 0))
        return f

    def _build_about(self):
        import tkinter as tk
        f = tk.Frame(self.content, bg=BG)
        card = tk.Frame(f, bg=CARD, highlightthickness=1,
                        highlightbackground=LINE, highlightcolor=LINE)
        card.pack(fill="x", pady=(2, 0))
        tk.Label(card, text="Deckster", bg=CARD, fg=INK,
                 font=("Segoe UI Semibold", 16)).pack(anchor="w", padx=16, pady=(14, 0))
        self.about_ver = tk.Label(card, text="", bg=CARD, fg=INK2, font=("Segoe UI", 10))
        self.about_ver.pack(anchor="w", padx=16, pady=(2, 2))
        tk.Label(card, text="Turn your phone into a Windows audio mixer.",
                 bg=CARD, fg=SUB, font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(0, 14))
        return f

    def _toggle_row(self, parent, text, sub, cmd):
        import tkinter as tk
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", padx=14, pady=10)
        txt = tk.Frame(row, bg=CARD)
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text=text, bg=CARD, fg=INK, font=("Segoe UI", 10, "bold"),
                 anchor="w").pack(fill="x")
        tk.Label(txt, text=sub, bg=CARD, fg=SUB, font=("Segoe UI", 8),
                 anchor="w").pack(fill="x")
        sw = ToggleSwitch(row, cmd, bg=CARD)
        sw.pack(side="right")
        return sw

    def _btn(self, parent, text, cmd, fg=INK):  # kept for compatibility
        return PillButton(parent, text, cmd, bg=parent["bg"]).cv

    # ---- actions (run on the UI thread; Admin marshals to the server loop) ----
    def _set_mode(self, m):
        try: self.admin.set_mode(m)
        except Exception: log.exception("set_mode")
    def _toggle_secure(self, on):
        try: self.admin.set_secure(bool(on))
        except Exception: log.exception("set_secure")
    def _toggle_autostart(self, on):
        try: self.admin.set_autostart(bool(on))
        except Exception: log.exception("set_autostart")
    def _new_code(self):
        try: self.admin.refresh_code()
        except Exception: log.exception("refresh_code")
    def _revoke(self):
        sel = self.devices.curselection()
        if sel and sel[0] < len(self._dev_ids):
            try: self.admin.revoke(self._dev_ids[sel[0]])
            except Exception: log.exception("revoke")
    def _revoke_all(self):
        try: self.admin.revoke_all()
        except Exception: log.exception("revoke_all")
    def _allow_firewall(self):
        try: self.admin.allow_firewall()      # pops one UAC prompt; user accepts
        except Exception: log.exception("allow_firewall")
    def _import_soundboard_clip(self):
        if self.soundboard is None:
            return
        try:
            from tkinter import filedialog
            path = filedialog.askopenfilename(title="Choose soundboard clip",
                                              filetypes=[("Audio clips", "*.wav *.mp3 *.ogg *.flac")])
            if path:
                self.soundboard.import_clip(Path(path))
        except Exception: log.exception("import soundboard clip")
    def _remove_soundboard_clip(self):
        if self.soundboard is None:
            return
        sel = self.soundboard_clips.curselection()
        if sel and sel[0] < len(self._soundboard_clip_ids):
            try: self.soundboard.remove_clip(self._soundboard_clip_ids[sel[0]])
            except Exception: log.exception("remove soundboard clip")
    def _restore_soundboard_defaults(self):
        if self.soundboard is None:
            return
        try: self.soundboard.restore_defaults(reset=True)
        except Exception: log.exception("restore default soundboard clips")

    def _hide(self):
        self.root.withdraw()
    def _show(self):
        self.root.deiconify(); self.root.lift(); self.root.focus_force()
    def _quit(self):
        self.stop_event.set()      # triggers full app shutdown; _pump then closes the window

    # ---- periodic UI update + tray command pump ---------------------------
    def _pump(self):
        try:
            while True:
                cmd = self.cmd_queue.get_nowait()
                if cmd == "show": self._show()
                elif cmd == "quit": self.stop_event.set()
        except queue.Empty:
            pass
        if self.stop_event.is_set():
            try: self.root.destroy()
            except Exception: pass
            return
        self.root.after(150, self._pump)

    def _refresh(self):
        try:
            s = self.admin.state()
            ver = "v" + str(s.get("version", ""))
            self.ver.config(text=ver)
            if hasattr(self, "about_ver"): self.about_ver.config(text=ver)
            lan = s.get("mode") == "lan"
            self.status.set("Wi-Fi · on your LAN" if lan else "USB · off-network",
                            AMBER if lan else GREEN)
            self.url.config(text=str(s.get("connectUrl", "")))
            self.code.config(text=" ".join(str(s.get("pairCode", "")) or "------"))
            self.btn_usb.set_active(not lan)
            self.btn_wifi.set_active(lan)
            self.secure_toggle.set(bool(s.get("secure")))
            self.autostart_toggle.set(bool(s.get("autostart")))
            self._set_firewall_warning(bool(s.get("firewallNeeded")))
            self._fill_devices(s.get("devices", []))
            self.routing_panel.update_snapshot(self.admin.soundboard_state())
            self._fill_soundboard_clips()
            self._update_qr(s.get("qrPath", ""))
        except Exception:  # noqa: BLE001
            log.exception("window refresh")
        self.root.after(2000, self._refresh)

    def _set_firewall_warning(self, needed: bool):
        if needed == self._fw_shown:
            return
        self._fw_shown = needed
        if needed:
            self.fw_frame.pack(in_=self._fw_parent, fill="x", pady=(12, 0))
        else:
            self.fw_frame.pack_forget()

    def _fill_devices(self, devices):
        ids = [d.get("id") for d in devices]
        names = [d.get("name") for d in devices]
        if ids == self._dev_ids and names == getattr(self, "_dev_names", []):
            return
        self._dev_ids = ids
        self._dev_names = names
        self.devices.delete(0, "end")
        if not devices:
            self.devices.insert("end", "  No devices paired yet")
            self._dev_ids = []
        else:
            for d in devices:
                self.devices.insert("end", "  " + str(d.get("name", "Device")))

    def _fill_soundboard_clips(self):
        if not hasattr(self, "soundboard_clips"):
            return
        clips = self.soundboard.snapshot().get("clips", []) if self.soundboard is not None else []
        ids = [str(c.get("id")) for c in clips]
        appearance = [(c.get("label"), c.get("emoji"), c.get("voice"), c.get("ears")) for c in clips]
        if ids == self._soundboard_clip_ids and appearance == getattr(self, "_clip_appearance", []):
            return
        self._soundboard_clip_ids = ids
        self._clip_appearance = appearance
        self.soundboard_clips.delete(0, "end")
        if not clips:
            self.soundboard_clips.insert("end", "  No clips yet — add a WAV, MP3, OGG, or FLAC file")
        else:
            for clip in clips:
                routes = []
                if clip.get("voice"): routes.append("Others")
                if clip.get("ears"): routes.append("Me")
                self.soundboard_clips.insert("end", "  " + str(clip.get("emoji") or "♪") + "  "
                                             + str(clip.get("label", "Untitled")) + "  ·  "
                                             + "+".join(routes or ["Muted"]))

    def _update_qr(self, path):
        if not path or path == self._qr_shown:
            return
        try:
            from PIL import Image, ImageTk
            img = Image.open(path).convert("RGB").resize((168, 168), Image.NEAREST)
            self._qr_img = ImageTk.PhotoImage(img)
            self.qr_label.config(image=self._qr_img)
            self._qr_shown = path
        except Exception:  # noqa: BLE001
            pass

    def run(self):
        self.root.mainloop()


def run_window(admin, stop_event, cmd_queue, icon_path=None, soundboard=None, start_hidden=False):
    """Entry for the window thread. Best-effort: never crash the app if Tk is absent."""
    try:
        DecksterWindow(admin, stop_event, cmd_queue, icon_path, soundboard, start_hidden).run()
    except Exception as exc:  # noqa: BLE001
        log.info("window unavailable (%s); running with tray only", exc)
