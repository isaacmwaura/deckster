"""Deckster's own control-panel window (Tkinter).

A self-contained native window so the exe doesn't depend on a browser: it shows the
pairing QR + code, the connect URL, the USB/Wi-Fi + TLS toggles, paired devices, and
start-with-Windows. It talks directly to the in-process `Admin`, so no HTTP/browser.

Runs on its own UI thread (like the tray). Closing the window hides it to the tray
(the server keeps running in the background); Quit stops everything. A small queue
carries tray->window commands ("show"/"quit") so the tray thread never touches Tk.
"""
from __future__ import annotations

import queue

from .log import get_logger

log = get_logger("window")

BG = "#0B0E14"; CARD = "#14161B"; CARD2 = "#191C22"; LINE = "#2A2F3A"
INK = "#F2F3F5"; INK2 = "#C2C6CF"; SUB = "#9A9EA8"
ACCENT = "#56C2FF"; GREEN = "#4DDB7F"; AMBER = "#FFB84D"; RED = "#E05A5A"


class DecksterWindow:
    def __init__(self, admin, stop_event, cmd_queue: "queue.Queue", icon_path: str | None = None):
        import tkinter as tk

        self.admin = admin
        self.stop_event = stop_event
        self.cmd_queue = cmd_queue
        self._qr_img = None
        self._qr_shown = ""

        self.root = tk.Tk()
        self.root.title("Deckster")
        self.root.configure(bg=BG)
        self.root.geometry("470x690")
        self.root.minsize(440, 600)
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

        pad = {"padx": 18}
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", pady=(16, 6), **pad)
        tk.Label(header, text="Deckster", bg=BG, fg=INK,
                 font=("Segoe UI", 18, "bold")).pack(side="left")
        self.ver = tk.Label(header, text="", bg=BG, fg=SUB, font=("Segoe UI", 9))
        self.ver.pack(side="left", padx=6, pady=(6, 0))

        self.status = tk.Label(self.root, text="", bg=BG, fg=GREEN,
                               font=("Segoe UI", 10, "bold"), anchor="w")
        self.status.pack(fill="x", **pad)

        # QR + code
        qr_card = tk.Frame(self.root, bg=CARD)
        qr_card.pack(fill="x", pady=8, **pad)
        self.qr_label = tk.Label(qr_card, bg="#FFFFFF")
        self.qr_label.pack(pady=12)
        self.code = tk.Label(qr_card, text="— — — — — —", bg=CARD, fg=INK,
                             font=("Consolas", 20, "bold"))
        self.code.pack()
        tk.Label(qr_card, text="Scan the QR on the phone, or enter this code",
                 bg=CARD, fg=SUB, font=("Segoe UI", 9)).pack(pady=(0, 6))
        self._btn(qr_card, "New code", self._new_code).pack(pady=(0, 12))

        # connect url
        self.url = tk.Label(self.root, text="", bg=BG, fg=ACCENT,
                            font=("Consolas", 10), anchor="w")
        self.url.pack(fill="x", **pad)

        # mode toggle
        mode = tk.Frame(self.root, bg=BG)
        mode.pack(fill="x", pady=(10, 4), **pad)
        self.btn_usb = self._btn(mode, "USB · secure", lambda: self._set_mode("loopback"))
        self.btn_usb.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_wifi = self._btn(mode, "Wi-Fi", lambda: self._set_mode("lan"))
        self.btn_wifi.pack(side="left", expand=True, fill="x", padx=(4, 0))

        # toggles
        self.var_secure = tk.IntVar()
        self.var_autostart = tk.IntVar()
        self._check("Secure connection (TLS)", self.var_secure, self._toggle_secure)
        self._check("Start with Windows", self.var_autostart, self._toggle_autostart)

        # devices
        tk.Label(self.root, text="PAIRED DEVICES", bg=BG, fg=SUB,
                 font=("Segoe UI", 8, "bold")).pack(fill="x", pady=(10, 2), **pad)
        dev = tk.Frame(self.root, bg=BG)
        dev.pack(fill="both", expand=True, **pad)
        self.devices = tk.Listbox(dev, bg=CARD, fg=INK2, height=4, borderwidth=0,
                                  highlightthickness=0, selectbackground=CARD2,
                                  activestyle="none", font=("Segoe UI", 10))
        self.devices.pack(side="left", fill="both", expand=True)
        self._dev_ids: list[str] = []
        devbtns = tk.Frame(dev, bg=BG)
        devbtns.pack(side="left", fill="y", padx=(8, 0))
        self._btn(devbtns, "Revoke", self._revoke, fg=RED).pack(fill="x", pady=(0, 4))
        self._btn(devbtns, "Revoke all", self._revoke_all, fg=RED).pack(fill="x")

        # footer
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", pady=12, **pad)
        self._btn(foot, "Hide to tray", self._hide).pack(side="left")
        self._btn(foot, "Quit", self._quit, fg=RED).pack(side="right")

    def _btn(self, parent, text, cmd, fg=INK):
        import tkinter as tk
        return tk.Button(parent, text=text, command=cmd, bg=CARD2, fg=fg,
                         activebackground=LINE, activeforeground=fg, relief="flat",
                         font=("Segoe UI", 10, "bold"), borderwidth=0, padx=12, pady=8,
                         cursor="hand2")

    def _check(self, text, var, cmd):
        import tkinter as tk
        c = tk.Checkbutton(self.root, text=text, variable=var, command=cmd,
                           bg=BG, fg=INK2, selectcolor=CARD, activebackground=BG,
                           activeforeground=INK, font=("Segoe UI", 10),
                           anchor="w", borderwidth=0, highlightthickness=0)
        c.pack(fill="x", padx=16, pady=1)
        return c

    # ---- actions (run on the UI thread; Admin marshals to the server loop) ----
    def _set_mode(self, m):
        try: self.admin.set_mode(m)
        except Exception: log.exception("set_mode")
    def _toggle_secure(self):
        try: self.admin.set_secure(bool(self.var_secure.get()))
        except Exception: log.exception("set_secure")
    def _toggle_autostart(self):
        try: self.admin.set_autostart(bool(self.var_autostart.get()))
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
            self.ver.config(text="v" + str(s.get("version", "")))
            lan = s.get("mode") == "lan"
            self.status.config(
                text=("● Wi-Fi · on your LAN" if lan else "● USB · off-network"),
                fg=(AMBER if lan else GREEN))
            self.url.config(text=str(s.get("connectUrl", "")))
            self.code.config(text=" ".join(str(s.get("pairCode", "")) or "------"))
            self.btn_usb.config(bg=(CARD2 if lan else ACCENT), fg=(INK if lan else "#08121A"))
            self.btn_wifi.config(bg=(ACCENT if lan else CARD2), fg=("#08121A" if lan else INK))
            self.var_secure.set(1 if s.get("secure") else 0)
            self.var_autostart.set(1 if s.get("autostart") else 0)
            self._fill_devices(s.get("devices", []))
            self._update_qr(s.get("qrPath", ""))
        except Exception:  # noqa: BLE001
            log.exception("window refresh")
        self.root.after(2000, self._refresh)

    def _fill_devices(self, devices):
        ids = [d.get("id") for d in devices]
        if ids == self._dev_ids:
            return
        self._dev_ids = ids
        self.devices.delete(0, "end")
        if not devices:
            self.devices.insert("end", "  No devices paired yet")
            self._dev_ids = []
        else:
            for d in devices:
                self.devices.insert("end", "  " + str(d.get("name", "Device")))

    def _update_qr(self, path):
        if not path or path == self._qr_shown:
            return
        try:
            from PIL import Image, ImageTk
            img = Image.open(path).convert("RGB").resize((200, 200), Image.NEAREST)
            self._qr_img = ImageTk.PhotoImage(img)
            self.qr_label.config(image=self._qr_img)
            self._qr_shown = path
        except Exception:  # noqa: BLE001
            pass

    def run(self):
        self.root.mainloop()


def run_window(admin, stop_event, cmd_queue, icon_path=None):
    """Entry for the window thread. Best-effort: never crash the app if Tk is absent."""
    try:
        DecksterWindow(admin, stop_event, cmd_queue, icon_path).run()
    except Exception as exc:  # noqa: BLE001
        log.info("window unavailable (%s); running with tray only", exc)
