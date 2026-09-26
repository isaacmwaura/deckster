"""Guided routing: editable source/destination cards connected by live preview wires."""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

from .routing import ROUTE_KEYS, cable_pairs, is_virtual, recommend_route, route_issue, receiving_microphone
from .window import BG, CARD, CARD2, LINE, INK, INK2, SUB, ACCENT, ACCENT_INK, GREEN, AMBER, RED


class RoutingPanel(tk.Frame):
    def __init__(self, parent, admin):
        super().__init__(parent, bg=BG)
        self.admin = admin
        self.snapshot = {"config": {}, "inputs": [], "outputs": []}
        self.draft = dict.fromkeys(ROUTE_KEYS, "")
        self.dirty = False
        self._signature = None
        self._message_until = 0.0
        self.boxes, self.options, self.nodes = {}, {}, {}

        # Scroll only this page on compact displays; controls never disappear.
        scroll = tk.Canvas(self, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=scroll.yview)
        scroll.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        scroll.pack(side="left", fill="both", expand=True)
        body = tk.Frame(scroll, bg=BG)
        body_id = scroll.create_window(0, 0, window=body, anchor="nw")
        scroll.bind("<Configure>", lambda e: scroll.itemconfig(body_id, width=e.width))
        body.bind("<Configure>", lambda _e: scroll.configure(scrollregion=scroll.bbox("all")))
        self.scroll, self.body = scroll, body

        self._label(body, "Choose who hears your audio", INK, 15, True).pack(anchor="w")
        self._label(body, "Follow the arrows. Changes are a preview until you click Connect these devices.",
                    INK2, 9).pack(anchor="w", pady=(3, 12))
        actions = tk.Frame(body, bg=BG)
        actions.pack(fill="x", pady=(0, 10))
        self.recommend_button = self._button(actions, "Use recommended setup", self.recommend)
        self.recommend_button.pack(side="left")
        self.connect_button = self._button(actions, "Connect these devices", self.connect, primary=True)
        self.connect_button.pack(side="left", padx=8)
        self._button(actions, "Reset preview", self.reset).pack(side="left")

        self.status = self._label(body, "Choose recommended setup to begin.", AMBER, 9, True)
        self.status.pack(fill="x", pady=(0, 8))
        self.diagram = tk.Canvas(body, height=306, bg=BG, highlightthickness=0)
        self.diagram.pack(fill="x")
        self.diagram.bind("<Configure>", self._layout)
        self._node("mic", "1  Your microphone", "Your voice enters here", "inputId", ACCENT)
        self._node("others", "2  Other people hear · Voice", "Your voice + clips sent to apps", "voiceOutputId", GREEN)
        self._node("clips", "Your sound clips", "Tap a pad on your phone", None, ACCENT)
        self._node("you", "3  You hear clips · Ears", "Optional · sound clips only", "earsOutputId", AMBER)
        self.clip_summary = self._label(self.nodes["clips"][0], "Choose each clip's destinations on the phone.",
                                        INK2, 9, bg=CARD)
        self.clip_summary.pack(fill="x", padx=12, pady=5)

        finish = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=GREEN)
        finish.pack(fill="x", pady=(4, 10))
        self._label(finish, "4  In your call, game, or OBS", INK, 11, True, CARD).pack(anchor="w", padx=12, pady=(10, 2))
        self._label(finish, "Open its audio settings and choose this as its microphone:", INK2, 9, bg=CARD).pack(anchor="w", padx=12)
        line = tk.Frame(finish, bg=CARD)
        line.pack(fill="x", padx=12, pady=7)
        self.app_mic = tk.StringVar()
        tk.Entry(line, textvariable=self.app_mic, state="readonly", readonlybackground=CARD2,
                 fg=GREEN, relief="flat", font=("Segoe UI", 10), borderwidth=8).pack(side="left", fill="x", expand=True)
        self.copy_button = self._button(line, "Copy name", self.copy_name)
        self.copy_button.pack(side="right", padx=(8, 0))
        self._label(finish, "CABLE Input is the entrance; CABLE Output is the exit. They are two ends of the same cable.",
                    SUB, 9, bg=CARD).pack(fill="x", padx=12, pady=(0, 10))

        test = tk.Frame(body, bg=CARD)
        test.pack(fill="x", pady=(0, 10))
        self._label(test, "Check each destination", INK, 11, True, CARD).pack(anchor="w", padx=12, pady=(10, 4))
        row = tk.Frame(test, bg=CARD)
        row.pack(fill="x", padx=12, pady=(0, 6))
        self.test_others = self._button(row, "Test what others hear", lambda: self.test("voice"))
        self.test_others.pack(side="left")
        self.test_you = self._button(row, "Test what I hear", lambda: self.test("ears"))
        self.test_you.pack(side="left", padx=8)
        self._label(test, "Others: open your call/game's mic test to hear the tone. You: listen on the device in box 3.",
                    SUB, 9, bg=CARD).pack(fill="x", padx=12, pady=(0, 10))
        self.help = self._label(body, "", AMBER, 9)
        self.help.pack(fill="x", pady=(0, 10))

        def wheel(event):
            scroll.yview_scroll(-int(event.delta / 120), "units")
            return "break"
        def bind_children(widget):
            # Keep scrolling local to this page, and leave dropdown scrolling alone.
            if not isinstance(widget, ttk.Combobox):
                widget.bind("<MouseWheel>", wheel, add="+")
            for child in widget.winfo_children(): bind_children(child)
        bind_children(body)
        self.update_snapshot(self.admin.soundboard_state())

    @staticmethod
    def _label(parent, text, color=INK2, size=9, bold=False, bg=BG):
        label = tk.Label(parent, text=text, bg=bg, fg=color, anchor="w", justify="left",
                         font=("Segoe UI", size, "bold" if bold else "normal"))
        label.bind("<Configure>", lambda e: label.configure(wraplength=max(120, e.width - 4)))
        return label

    @staticmethod
    def _button(parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, relief="flat", borderwidth=0,
                         bg=ACCENT if primary else CARD2, fg=ACCENT_INK if primary else INK,
                         activebackground=LINE, activeforeground=INK, disabledforeground=SUB,
                         padx=12, pady=9, font=("Segoe UI", 9, "bold"), cursor="hand2")

    def _node(self, key, title, subtitle, config_key, color):
        card = tk.Frame(self.diagram, bg=CARD, highlightthickness=1, highlightbackground=color)
        self._label(card, title, color, 10, True, CARD).pack(fill="x", padx=12, pady=(11, 3))
        self._label(card, subtitle, INK2, 9, bg=CARD).pack(fill="x", padx=12)
        if config_key:
            box = ttk.Combobox(card, state="readonly", width=24, font=("Segoe UI", 9))
            box.pack(fill="x", padx=12, pady=(8, 10))
            box.bind("<<ComboboxSelected>>", lambda _e, k=config_key: self.choose(k))
            self.boxes[config_key] = box
        item = self.diagram.create_window(0, 0, window=card, anchor="nw")
        self.nodes[key] = (card, item)

    def _layout(self, event=None):
        w = max(520, event.width if event else self.diagram.winfo_width())
        width = (w - 72) / 2
        right = w - width - 2
        for name, x, y in (("mic", 2, 8), ("others", right, 8),
                           ("clips", 2, 182), ("you", right, 182)):
            self.diagram.coords(self.nodes[name][1], x, y)
            self.diagram.itemconfigure(self.nodes[name][1], width=width, height=120)
        self.diagram.delete("wire")
        ready = (not self.dirty and not route_issue(self.draft, self.snapshot)
                 and self.snapshot.get("runtime") == "ready")
        dash = () if ready else (5, 4)
        left = width + 3
        mid = (left + right) / 2
        color = GREEN if ready else SUB
        self.diagram.create_line(left, 66, right - 5, 66, width=3, fill=color,
                                 arrow="last", dash=dash, tags="wire")
        self.diagram.create_line(left, 216, mid - 9, 216, mid - 9, 104, right - 5, 104,
                                 width=3, fill=color, arrow="last", dash=dash, tags="wire")
        monitor = bool(self.draft.get("earsOutputId"))
        self.diagram.create_line(left, 268, right - 5, 268, width=3,
                                 fill=AMBER if monitor else LINE, arrow="last",
                                 dash=dash if monitor else (3, 5), tags="wire")
        self.diagram.create_text(mid, 156, text="Preview" if self.dirty else "Audio flows →",
                                 fill=SUB, font=("Segoe UI", 8), tags="wire")
        self.diagram.tag_lower("wire")

    def update_snapshot(self, snapshot):
        self.snapshot = snapshot
        saved = {k: str(snapshot.get("config", {}).get(k, "")) for k in ROUTE_KEYS}
        if not self.dirty:
            self.draft = saved
        self.dirty = self.draft != saved
        signature = (tuple((d["id"], d["name"]) for d in snapshot.get("inputs", [])),
                     tuple((d["id"], d["name"]) for d in snapshot.get("outputs", [])), tuple(self.draft.values()))
        if signature != self._signature:
            self._signature = signature
            self._populate()
        clips = snapshot.get("clips", [])
        self.clip_summary.config(text=f"{sum(bool(c.get('voice')) for c in clips)} clips to others  ·  "
                                     f"{sum(bool(c.get('ears')) for c in clips)} clips to you")
        self._render()

    def _populate(self):
        inputs, outputs = self.snapshot.get("inputs", []), self.snapshot.get("outputs", [])
        choices = {"inputId": [d for d in inputs if not is_virtual(d)],
                   "voiceOutputId": [p[0] for p in cable_pairs(outputs, inputs)],
                   "earsOutputId": [d for d in outputs if not is_virtual(d)]}
        for key, box in self.boxes.items():
            options = [("", "Off · I don't need to hear clips" if key == "earsOutputId" else
                        "Choose your microphone" if key == "inputId" else "Choose a virtual cable")]
            options += [(d["id"], d["name"]) for d in choices[key]]
            value = self.draft[key]
            if value and value not in {id_ for id_, _ in options}:
                old = next((d["name"] for d in inputs + outputs if d["id"] == value), "Device disconnected")
                options.append((value, "Needs attention · " + old))
            self.options[key] = options
            box["values"] = [label for _, label in options]
            box.current(next(i for i, (id_, _) in enumerate(options) if id_ == value))

    def choose(self, key):
        self.draft[key] = self.options[key][self.boxes[key].current()][0]
        self.dirty = any(self.draft[k] != self.snapshot.get("config", {}).get(k, "") for k in ROUTE_KEYS)
        self._message_until = 0
        self._render()

    def recommend(self):
        try:
            self.snapshot = self.admin.soundboard_state()
            self.draft = recommend_route(self.snapshot)
            self.dirty = any(self.draft[k] != self.snapshot.get("config", {}).get(k, "") for k in ROUTE_KEYS)
            self._signature = None
            self._populate()
            self._message_until = 0
            self._render()
        except Exception as exc:
            self.message(str(exc), AMBER)

    def reset(self):
        self.dirty = False
        self._message_until = 0
        self.update_snapshot(self.admin.soundboard_state())

    def _render(self):
        issue = route_issue(self.draft, self.snapshot)
        ready = self.snapshot.get("runtime") == "ready" and not issue and not self.dirty
        self.connect_button.configure(state="disabled" if issue else "normal")
        self.test_others.configure(state="normal" if ready else "disabled")
        self.test_you.configure(state="normal" if ready and self.draft["earsOutputId"] else "disabled")
        self.copy_button.configure(state="disabled" if issue else "normal")
        self.app_mic.set(receiving_microphone(self.draft, self.snapshot))
        missing = not cable_pairs(self.snapshot.get("outputs", []), self.snapshot.get("inputs", []))
        self.help.configure(text="No virtual cable found. Install VB-CABLE from vb-audio.com/Cable, restart if requested, and return. Devices refresh automatically."
                            if missing else "A solid wire means connected on this PC. A dashed wire is a preview. Each phone pad lets you choose Others, Me, or both.")
        if time.monotonic() >= self._message_until:
            if self.dirty:
                text, color = (issue or "Preview ready · click Connect these devices to start this route."), AMBER
            elif issue:
                text, color = issue, AMBER
            elif self.snapshot.get("error"):
                text, color = "Couldn't connect: " + self.snapshot["error"], RED
            elif ready:
                text, color = "Connected on this PC · finish step 4 in your call/game, then test.", GREEN
            else:
                text, color = "Ready to connect · click Connect these devices.", INK2
            self.status.configure(text=text, fg=color)
        self._layout()

    def connect(self):
        fresh = self.admin.soundboard_state()
        issue = route_issue(self.draft, fresh)
        if issue:
            self.snapshot = fresh
            self._populate()
            self._render()
            self.message(issue, AMBER)
            return
        try:
            snap = self.admin.configure_soundboard(dict(self.draft))
            self.dirty = False
            self._message_until = 0
            self.update_snapshot(snap)
        except Exception as exc:
            self.message(str(exc), RED)

    def copy_name(self):
        self.clipboard_clear()
        self.clipboard_append(self.app_mic.get())
        self.message("Microphone name copied. Select that device in your call/game's audio settings.", GREEN)

    def test(self, bus):
        if self.dirty or route_issue(self.draft, self.snapshot):
            self.message("Connect your preview before testing.", AMBER)
            return
        try:
            self.admin.test_soundboard_route(bus)
            self.message("Short tone sent. Check your call/game's microphone test." if bus == "voice"
                         else "Short tone sent to your selected headphones/speakers.", GREEN)
        except Exception as exc:
            self.message(str(exc), RED)

    def message(self, text, color):
        self._message_until = time.monotonic() + 12
        self.status.configure(text=text, fg=color)
