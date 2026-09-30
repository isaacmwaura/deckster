"""Native desktop editor for Deckster's phone presentation."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .presentation import RevisionConflict

BG = "#10151d"
CARD = "#18212c"
INK = "#eff5fc"
SUB = "#a5b5c5"
ACCENT = "#7abaff"
SLOTS = 12


class PhoneLayoutPanel(tk.Frame):
    """One saved draft for page placement, mixer tiles, and sound pads."""

    def __init__(self, parent, admin):
        super().__init__(parent, bg=BG)
        self.admin = admin
        self.draft = None
        self.sessions = []
        self.clips = []
        self.devices = {}
        self.dirty = False
        self._drag_page = None
        self._drag_app = None
        self._drag_clip = None
        self._drag_slot = None
        self._slot_buttons = []
        self._icon_cache = {}
        self._mixer_tiles = []
        self._selected_app = None
        self._mixer_drag = None
        self._mixer_scroll = 0
        self._build()
        self.update_snapshot(self.admin.presentation_state())

    def _build(self):
        action_row = tk.Frame(self, bg=BG)
        action_row.pack(side="bottom", fill="x", pady=(8, 0))
        outer = tk.Canvas(self, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=outer.yview)
        outer.configure(yscrollcommand=bar.set)
        outer.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        body = tk.Frame(outer, bg=BG)
        win = outer.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _e: outer.configure(scrollregion=outer.bbox("all")))
        outer.bind("<Configure>", lambda e: outer.itemconfigure(win, width=e.width))
        self._scroll_canvas = outer
        # Bind only this panel's descendants: other settings pages retain
        # their own wheel behavior and destruction removes all bindings.
        def wheel(event):
            if event.widget is self.mixer_preview and self._mixer_scroll_max > 0:
                self._mixer_scroll = max(0, min(self._mixer_scroll_max, self._mixer_scroll - event.delta / 120 * 60))
                self._draw_mixer_preview()
                return "break"
            outer.yview_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
            return "break"
        self._wheel = wheel

        tk.Label(body, text="PHONE PAGE LAYOUT", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(2, 4))
        tk.Label(body, text="Drag any page to a position. Occupied positions swap; the center must remain filled.",
                 bg=BG, fg=SUB, font=("Segoe UI", 9), wraplength=620,
                 justify="left").pack(anchor="w", pady=(0, 5))
        self.map = tk.Canvas(body, height=242, bg=CARD, highlightthickness=0)
        self.map.pack(fill="x", pady=(0, 11))
        self.map.bind("<Configure>", lambda _e: self._draw_map() if self.draft else None)
        self.map.bind("<ButtonPress-1>", self._map_down)
        self.map.bind("<B1-Motion>", self._map_motion)
        self.map.bind("<ButtonRelease-1>", self._map_up)

        tk.Label(body, text="MIXER APPS", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(2, 4))
        tk.Label(body, text="Drag visible apps to reorder them. Hidden apps stay hidden when they reopen.",
                 bg=BG, fg=SUB, font=("Segoe UI", 9), wraplength=520,
                 justify="left").pack(anchor="w")
        self.mixer_preview = tk.Canvas(body, height=230, bg=CARD, highlightthickness=0)
        self.mixer_preview.pack(fill="x", pady=(5, 5))
        self.mixer_preview.bind("<Configure>", lambda _e: self._draw_mixer_preview() if self.draft else None)
        self.mixer_preview.bind("<ButtonPress-1>", self._mixer_down)
        self.mixer_preview.bind("<ButtonRelease-1>", self._mixer_up)
        self.mixer_preview.bind("<Button-3>", self._mixer_hide)
        app_row = tk.Frame(body, bg=BG)
        app_row.pack(fill="x", pady=(5, 12))
        self.visible = tk.Listbox(app_row, height=6, bg=CARD, fg=INK, selectbackground="#275887",
                                  highlightthickness=0, borderwidth=0, font=("Segoe UI", 10))
        self.visible.pack(side="left", fill="both", expand=True)
        self.visible.bind("<ButtonPress-1>", self._app_down)
        self.visible.bind("<ButtonRelease-1>", self._app_up)
        actions = tk.Frame(app_row, bg=BG)
        actions.pack(side="left", padx=8)
        tk.Button(actions, text="Hide →", command=self._hide_app).pack(fill="x", pady=3)
        tk.Button(actions, text="← Show", command=self._show_app).pack(fill="x", pady=3)
        self.hidden = tk.Listbox(app_row, height=6, bg=CARD, fg=SUB, selectbackground="#275887",
                                 highlightthickness=0, borderwidth=0, font=("Segoe UI", 10))
        self.hidden.pack(side="left", fill="both", expand=True)

        tk.Label(body, text="SOUNDBOARD · 12 PHONE PADS", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(2, 4))
        tk.Label(body, text="Drag a library sound onto a pad to replace it. Drag pads to swap; Remove clears only that position.",
                 bg=BG, fg=SUB, font=("Segoe UI", 9), wraplength=620,
                 justify="left").pack(anchor="w")
        sound_row = tk.Frame(body, bg=BG)
        self.sound_row = sound_row
        sound_row.pack(fill="x", pady=(6, 6))
        self.library = tk.Listbox(sound_row, height=10, width=19, bg=CARD, fg=INK,
                                  selectbackground="#275887", highlightthickness=0,
                                  borderwidth=0, font=("Segoe UI", 10))
        self.library.pack(side="left", fill="y")
        self.library.bind("<ButtonPress-1>", self._library_down)
        self.library.bind("<ButtonRelease-1>", self._library_up)
        phone = tk.Frame(sound_row, bg="#0b1118", highlightthickness=2,
                         highlightbackground="#7092ac")
        phone.pack(side="left", fill="both", expand=True, padx=(10, 0))
        tk.Label(phone, text="DECKSTER  /  SOUNDBOARD", bg="#0b1118", fg=ACCENT,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=8, pady=(5, 2))
        grid = tk.Frame(phone, bg="#0b1118")
        grid.pack(fill="both", expand=True, padx=5, pady=(0, 5))
        for i in range(SLOTS):
            button = tk.Button(grid, bg=CARD, fg=INK, relief="flat", width=7, height=2,
                               font=("Segoe UI", 9), cursor="hand2")
            button.grid(row=i // 4, column=i % 4, sticky="nsew", padx=2, pady=2)
            button.bind("<ButtonPress-1>", lambda _e, idx=i: self._slot_down(idx))
            button.bind("<ButtonRelease-1>", lambda e, idx=i: self._slot_up(e, idx))
            self._slot_buttons.append(button)
        for col in range(4):
            grid.columnconfigure(col, weight=1)
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", pady=(0, 12))
        tk.Button(row, text="Assign selected to selected pad", command=self._assign_selected).pack(side="left")
        tk.Button(row, text="Remove selected pad", command=self._clear_selected).pack(side="left", padx=8)
        self._selected_slot = 0
        for row_index in range(3):
            grid.rowconfigure(row_index, weight=1)

        tk.Button(action_row, text="Save configuration", command=self.save,
                  bg="#4a91d6", fg="white", relief="flat", padx=16, pady=7).pack(side="left")
        tk.Button(action_row, text="Discard changes", command=self.discard,
                  padx=12, pady=7).pack(side="left", padx=8)
        self.status = tk.Label(action_row, text="", bg=BG, fg=SUB, font=("Segoe UI", 9),
                               wraplength=205, justify="left")
        self.status.pack(side="left", padx=9)
        self._bind_wheel(self)

    def _bind_wheel(self, widget):
        widget.bind("<MouseWheel>", self._wheel)
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def update_snapshot(self, state):
        self.sessions = list(state.get("sessions", []))
        self.clips = list(state.get("clips", []))
        self.devices = dict(state.get("devices", {}))
        current = state.get("presentation") or {}
        if self.draft is None or not self.dirty:
            self.draft = {"revision": current.get("revision", 0),
                          "appOrder": list(current.get("appOrder", [])),
                          "hiddenApps": list(current.get("hiddenApps", [])),
                          "pages": dict(current.get("pages", {})),
                          "hideInteraction": current.get("hideInteraction", "drag"),
                          "padSlots": list(current.get("padSlots", [None] * SLOTS))}
        elif current.get("revision") != self.draft["revision"]:
            self.status.config(text="Layout changed on the phone. Discard and review before saving.", fg="#efaa75")
        self._draw()

    def _app_ids(self):
        live = [str(x["id"]) for x in self.sessions]
        return list(dict.fromkeys(self.draft["appOrder"] + live + self.draft["hiddenApps"]))

    def _app_name(self, app_id):
        return next((str(x.get("appLabel") or x.get("label") or app_id) for x in self.sessions
                     if str(x.get("id")) == app_id), app_id + " (offline)")

    def _clip_name(self, clip_id):
        clip = next((x for x in self.clips if str(x.get("id")) == clip_id), None)
        return (str(clip.get("emoji") or "♪") + " " + str(clip.get("label") or "Sound")) if clip else "Missing clip"

    def _icon_for(self, app_id, size=24):
        session = next((x for x in self.sessions if str(x.get("id")) == app_id), None)
        key = session.get("iconKey") if session else None
        if not key:
            return None
        cache_key = (key, size)
        if cache_key in self._icon_cache:
            return self._icon_cache[cache_key]
        try:
            from io import BytesIO
            from PIL import Image, ImageTk
            from .icons import ICONS
            raw = ICONS.get_png(key)
            icon = ImageTk.PhotoImage(Image.open(BytesIO(raw)).resize((size, size))) if raw else None
        except Exception:
            icon = None
        if icon is not None:
            self._icon_cache[cache_key] = icon
        return icon

    def _draw_mixer_preview(self):
        canvas = self.mixer_preview
        canvas.delete("all")
        self._mixer_tiles = []
        width = max(canvas.winfo_width(), 300)
        live = {str(session["id"]) for session in self.sessions}
        visible = [x for x in self._app_ids()
                   if x in live and x not in self.draft["hiddenApps"]]
        # Match the landscape phone: system rail, two app columns, then dial.
        scale = (width - 16) / 800
        height = round(360 * scale) + 14
        self._mixer_scroll_max = max(0, ((len(visible) + 1) // 2 * 114 - 283) * scale)
        self._mixer_scroll = min(self._mixer_scroll, self._mixer_scroll_max)
        if int(canvas.cget("height")) != height:
            canvas.configure(height=height)
        frame_left, frame_right = 8, width - 8
        canvas.create_rectangle(frame_left, 5, frame_right, height - 7,
                                fill="#0b1118", outline="#7092ac", width=2)
        def rect(x, y, w, h, **kw):
            canvas.create_rectangle(8 + x * scale, 5 + y * scale,
                                    8 + (x + w) * scale, 5 + (y + h) * scale, **kw)
        def text(x, y, value, color=INK, size=10, anchor="w"):
            canvas.create_text(8 + x * scale, 5 + y * scale, text=value,
                               fill=color, anchor=anchor, font=("Segoe UI", max(6, round(size * scale)), "bold"))
        text(12, 17, "Connected", "#7fe0a3")
        text(570, 17, "Soundboard    Mixer    Devices", SUB)
        rect(0, 34, 104, 326, fill="#101116", outline="#22252d")
        text(9, 51, "SYSTEM", SUB)
        for y, label in ((65, "MIC"), (208, "SPEAKERS")):
            rect(9, y, 86, 130, fill="#15171d", outline="#22252d")
            master = self.devices.get("micMaster" if label == "MIC" else "speakerMaster", {})
            text(52, y + 52, label, anchor="center", size=13)
            text(52, y + 80, "MUTED" if master.get("muted") else str(round(float(master.get("level", 1)) * 100)) + "%", anchor="center")
        text(118, 51, "APPS", SUB)
        rect(528, 34, 272, 326, fill="#101116", outline="#22252d")
        canvas.create_oval(8 + 616 * scale, 5 + 104 * scale, 8 + 784 * scale, 5 + 272 * scale, outline=ACCENT, width=3)
        selected = next((s for s in self.sessions if s["id"] == self._selected_app), None)
        text(700, 188, str(round(float((selected or {}).get("level", 1)) * 100)) + "%", size=32, anchor="center")
        text(562, 148, "App", ACCENT, anchor="center")
        text(562, 184, "Sys", SUB, anchor="center")
        text(562, 220, "Mic", SUB, anchor="center")
        tile_width = 193 * scale
        for idx, app_id in enumerate(visible):
            col, row = idx % 2, idx // 2
            x = 8 + (118 + col * 203) * scale
            y = 5 + (65 + row * 114) * scale - self._mixer_scroll
            tile_height = 104 * scale
            if y < 5 + 65 * scale or y + tile_height > height - 7:
                continue
            canvas.create_rectangle(x, y, x + tile_width, y + tile_height,
                                    fill="#304c64" if app_id == self._selected_app else "#1a2b3b",
                                    outline="#80aace" if app_id == self._selected_app else "#40576d")
            icon = self._icon_for(app_id)
            if icon:
                canvas.create_image(x + 20, y + 20, image=icon)
            else:
                canvas.create_rectangle(x + 5, y + 6, x + 28, y + 29,
                                        fill="#719dc0", outline="")
                canvas.create_text(x + 16, y + 17, text=self._app_name(app_id)[:1].upper(),
                                   fill="#0b1118", font=("Segoe UI", 9, "bold"))
            canvas.create_text(x + 37, y + 20, text=self._app_name(app_id)[:14],
                               anchor="w", fill=INK, font=("Segoe UI", 8, "bold"))
            session = next(s for s in self.sessions if str(s["id"]) == app_id)
            canvas.create_text(x + 10, y + tile_height - 18, text=str(round(float(session.get("level", 0)) * 100)) + "%",
                               anchor="w", fill=SUB if session.get("muted") else INK,
                               font=("Segoe UI", max(9, round(20 * scale)), "bold"))
            canvas.create_text(x + tile_width - 10, y + tile_height - 18, text="●   MIC", anchor="e", fill=SUB, font=("Segoe UI", 8))
            self._mixer_tiles.append((app_id, (x, y, x + tile_width, y + tile_height)))

    def _mixer_hit(self, x, y):
        return next((app_id for app_id, (x1, y1, x2, y2) in self._mixer_tiles
                     if x1 <= x <= x2 and y1 <= y <= y2), None)

    def _mixer_down(self, event):
        self._mixer_drag = self._mixer_hit(event.x, event.y)
        self._selected_app = self._mixer_drag
        self._draw_mixer_preview()

    def _mixer_up(self, event):
        source, self._mixer_drag = self._mixer_drag, None
        target = self._mixer_hit(event.x, event.y)
        if not source or not target or source == target:
            return
        live = {str(session["id"]) for session in self.sessions}
        visible = [x for x in self._app_ids()
                   if x in live and x not in self.draft["hiddenApps"]]
        visible.remove(source)
        visible.insert(visible.index(target), source)
        ordered = iter(visible)
        self.draft["appOrder"] = [next(ordered) if x in live and x not in self.draft["hiddenApps"] else x
                                  for x in self._app_ids()]
        self._changed()

    def _mixer_hide(self, event):
        app_id = self._mixer_hit(event.x, event.y)
        if app_id:
            self.draft["hiddenApps"] = list(dict.fromkeys(self.draft["hiddenApps"] + [app_id]))
            self._selected_app = None
            self._changed()

    def _draw(self):
        if self.draft is None:
            return
        prior_visible = (self._visible_ids[self.visible.curselection()[0]]
                         if self.visible.curselection() and hasattr(self, "_visible_ids") and
                         self.visible.curselection()[0] < len(self._visible_ids) else None)
        prior_hidden = (self._hidden_ids[self.hidden.curselection()[0]]
                        if self.hidden.curselection() and hasattr(self, "_hidden_ids") and
                        self.hidden.curselection()[0] < len(self._hidden_ids) else None)
        prior_library = (self._library_ids[self.library.curselection()[0]]
                         if self.library.curselection() and hasattr(self, "_library_ids") and
                         self.library.curselection()[0] < len(self._library_ids) else None)
        self._draw_map()
        self._draw_mixer_preview()
        visible = [x for x in self._app_ids() if x not in self.draft["hiddenApps"]]
        hidden = [x for x in self._app_ids() if x in self.draft["hiddenApps"]]
        self._visible_ids, self._hidden_ids = visible, hidden
        self.visible.delete(0, "end")
        self.hidden.delete(0, "end")
        for x in visible:
            self.visible.insert("end", "  " + self._app_name(x))
        if prior_visible in visible:
            self.visible.selection_set(visible.index(prior_visible))
        for x in hidden:
            self.hidden.insert("end", "  " + self._app_name(x))
        if prior_hidden in hidden:
            self.hidden.selection_set(hidden.index(prior_hidden))
        self._library_ids = [str(x["id"]) for x in self.clips if x["id"] not in self.draft["padSlots"]]
        self.library.delete(0, "end")
        for x in self._library_ids:
            self.library.insert("end", "  " + self._clip_name(x))
        if prior_library in self._library_ids:
            self.library.selection_set(self._library_ids.index(prior_library))
        for i, button in enumerate(self._slot_buttons):
            clip_id = self.draft["padSlots"][i]
            clip = next((c for c in self.clips if c["id"] == clip_id), None)
            text = "+" if clip is None else (str(clip.get("emoji") or "♪") + "\n" + str(clip.get("label") or "Untitled") + "\n" + " · ".join(x for x, yes in (("OTHERS", clip.get("voice")), ("ME", clip.get("ears"))) if yes))
            button.config(text=text, font=("Segoe UI Emoji", 10), wraplength=max(70, button.winfo_width() - 12),
                          bg="#29445f" if i == self._selected_slot else "#14161b")

    def _draw_map(self):
        c = self.map
        c.delete("all")
        w = max(c.winfo_width(), 280)
        center = w / 2
        offset = min(155, max(95, (w - 115) / 3))
        cardw = min(135, max(96, (w - 14) / 3 - 3))
        positions = {"center": (center, 121), "top": (center, 43),
                     "left": (center - offset, 121), "right": (center + offset, 121),
                     "bottom": (center, 199)}
        pages = {"mixer": "center", **self.draft["pages"]}
        page_at = {pos: name for name, pos in pages.items()}
        glyphs = {"mixer": "◉", "soundboard": "♪", "devices": "⌁", "media": "▶"}
        for pos, (x, y) in positions.items():
            name = page_at.get(pos)
            c.create_rectangle(x-cardw/2, y-34, x+cardw/2, y+34,
                               fill="#293d2a" if name == "mixer" else "#1e3040",
                               outline=ACCENT if name else "#344657", width=2)
            c.create_text(x, y-8, text=(glyphs[name]+"  "+name.title()) if name else "Empty",
                          fill=INK if name else SUB, font=("Segoe UI", 9, "bold"))
            c.create_text(x, y+13, text=pos.title(), fill=SUB, font=("Segoe UI", 8))

    def _map_position(self, event):
        x, y = event.x, event.y
        w = max(self.map.winfo_width(), 280)
        center = w / 2
        offset = min(155, max(95, (w - 115) / 3))
        cardw = min(135, max(96, (w - 14) / 3 - 3))
        for pos, (px, py) in {"center": (center, 121), "top": (center, 43), "left": (center - offset, 121),
                              "right": (center + offset, 121), "bottom": (center, 199)}.items():
            if abs(x - px) <= cardw / 2 and abs(y - py) <= 34:
                return pos
        return None

    def _map_down(self, event):
        pos = self._map_position(event)
        pages = {"mixer": "center", **self.draft["pages"]}
        self._drag_page = next((name for name, p in pages.items() if p == pos), None)

    def _map_motion(self, event):
        self.map.config(cursor="fleur" if self._drag_page else "")

    def _map_up(self, event):
        page, self._drag_page = self._drag_page, None
        target = self._map_position(event)
        if page == "mixer" or target == "center":
            self.draft["pages"] = {"mixer": "center", **self.draft["pages"]}
        if not page or not target or target == self.draft["pages"][page]:
            return
        prior = self.draft["pages"][page]
        occupant = next((name for name, pos in self.draft["pages"].items() if pos == target), None)
        self.draft["pages"][page] = target
        if occupant:
            self.draft["pages"][occupant] = prior
        self._changed()

    def _app_down(self, event):
        self._drag_app = self.visible.nearest(event.y)

    def _app_up(self, event):
        source, self._drag_app = self._drag_app, None
        target = self.visible.nearest(event.y)
        if source is None or not (0 <= source < len(self._visible_ids)) or target == source:
            return
        order = list(self._visible_ids)
        item = order.pop(source)
        order.insert(target, item)
        self.draft["appOrder"] = order + [x for x in self._app_ids() if x not in order]
        self._changed()

    def _hide_app(self):
        sel = self.visible.curselection()
        if sel:
            app_id = self._visible_ids[sel[0]]
            self.draft["hiddenApps"] = list(dict.fromkeys(self.draft["hiddenApps"] + [app_id]))
            self._changed()

    def _show_app(self):
        sel = self.hidden.curselection()
        if sel:
            app_id = self._hidden_ids[sel[0]]
            self.draft["hiddenApps"] = [x for x in self.draft["hiddenApps"] if x != app_id]
            self._changed()

    def _library_down(self, event):
        idx = self.library.nearest(event.y)
        self._drag_clip = self._library_ids[idx] if idx < len(self._library_ids) else None
        self._library_press = (event.x_root, event.y_root)

    def _library_up(self, event):
        target = self._slot_under_pointer(event.x_root, event.y_root)
        if target is not None and self._drag_clip:
            self._put_clip(target, self._drag_clip)
        elif self._drag_clip and abs(event.x_root - self._library_press[0]) + abs(event.y_root - self._library_press[1]) < 8:
            self._audition(self._drag_clip)
        self._drag_clip = None

    def _audition(self, clip_id):
        try:
            self.admin.audition_soundboard_clip(clip_id)
            self.status.config(text="Playing on your headphones/speakers", fg="#82d5a0")
        except Exception as exc:
            self.status.config(text=str(exc), fg="#ef9292")

    def _slot_under_pointer(self, x, y):
        for i, button in enumerate(self._slot_buttons):
            if (button.winfo_rootx() <= x < button.winfo_rootx() + button.winfo_width() and
                    button.winfo_rooty() <= y < button.winfo_rooty() + button.winfo_height()):
                return i
        return None

    def _slot_down(self, idx):
        self._selected_slot = idx
        self._drag_slot = idx
        self._draw()

    def _slot_up(self, event, idx):
        target = self._slot_under_pointer(event.x_root, event.y_root)
        source, self._drag_slot = self._drag_slot, None
        if source is not None and source == target:
            clip_id = self.draft["padSlots"][source]
            if clip_id:
                self._audition(clip_id)
            return
        if source is None or target is None:
            return
        slots = self.draft["padSlots"]
        slots[source], slots[target] = slots[target], slots[source]
        self._selected_slot = target
        self._changed()

    def _put_clip(self, target, clip_id):
        slots = self.draft["padSlots"]
        source = next((i for i, x in enumerate(slots) if x == clip_id), None)
        if source is not None and source != target:
            slots[source] = slots[target]
        slots[target] = clip_id
        self._selected_slot = target
        self._changed()

    def _assign_selected(self):
        sel = self.library.curselection()
        if sel and sel[0] < len(self._library_ids):
            self._put_clip(self._selected_slot, self._library_ids[sel[0]])

    def _clear_selected(self):
        self.draft["padSlots"][self._selected_slot] = None
        self._changed()

    def _changed(self):
        self.dirty = True
        self.status.config(text="Unsaved changes", fg=SUB)
        self._draw()

    def save(self):
        if not self.dirty:
            return
        changes = {key: self.draft[key] for key in ("appOrder", "hiddenApps", "pages", "padSlots", "hideInteraction")}
        try:
            saved = self.admin.configure_presentation(changes, self.draft["revision"])
        except RevisionConflict:
            self.status.config(text="Layout changed on the phone. Discard and review before saving.", fg="#efaa75")
            return
        except Exception as exc:
            self.status.config(text=str(exc), fg="#ef9292")
            return
        self.draft = saved
        self.dirty = False
        self.status.config(text="Saved to phone", fg="#82d5a0")
        self._draw()

    def discard(self):
        self.dirty = False
        self.draft = None
        self.update_snapshot(self.admin.presentation_state())
        self.status.config(text="Changes discarded", fg=SUB)
