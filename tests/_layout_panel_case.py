"""Hidden Tk interaction checks, run in a separate process for Tcl thread safety."""
from __future__ import annotations

import gc
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.layout_panel import PhoneLayoutPanel
from agent.presentation import PresentationService


class Library:
    def snapshot(self):
        return {"clips": [{"id": "bell", "label": "Bell", "emoji": "🔔"},
                          {"id": "horn", "label": "Horn", "emoji": "📣"}],
                "config": {"layout": "a"}}


class Admin:
    def __init__(self, service):
        self.service = service

    def presentation_state(self):
        return {"presentation": self.service.snapshot(),
                "clips": Library().snapshot()["clips"],
                "sessions": [{"id": f"app-{i}", "appLabel": f"App {i}"} for i in range(10)]}

    def configure_presentation(self, changes, base_revision):
        return self.service.update(changes, base_revision)


def exercise_hidden_layout_drag_save_and_remote_conflict(tmp_path):
    root = tk.Tk()
    root.withdraw()
    try:
        root.geometry("580x700")
        service = PresentationService(tmp_path, Library())
        service.update({"appOrder": ["offline", "app-0"]}, 0)
        panel = PhoneLayoutPanel(root, Admin(service))
        panel.pack(fill="both", expand=True)
        root.update_idletasks()
        assert panel.sound_row.winfo_reqwidth() <= 580
        assert panel.status.master is not panel.sound_row.master
        assert "offline" in panel._visible_ids

        # Map drag moves the page to a free side and persists on Save.
        pages = panel.draft["pages"]
        assert pages["soundboard"] == "top"
        center = max(panel.map.winfo_width(), 280) / 2
        offset = min(155, max(95, (max(panel.map.winfo_width(), 280) - 115) / 3))
        panel._map_down(SimpleNamespace(x=center, y=43))
        panel._map_up(SimpleNamespace(x=center - offset, y=121))
        assert panel.draft["pages"]["soundboard"] == "left"

        # Preview follows the phone's two columns; wheel exposes later rows.
        panel._draw_mixer_preview()
        assert len(panel._mixer_tiles) == 4
        first = panel._mixer_tiles[0][1]
        panel._mixer_down(SimpleNamespace(x=(first[0] + first[2]) / 2,
                                          y=(first[1] + first[3]) / 2))
        panel._wheel(SimpleNamespace(widget=panel.mixer_preview, delta=-1200))
        assert panel._mixer_scroll > 0
        last = panel._mixer_tiles[-1][1]
        panel._mixer_up(SimpleNamespace(x=(last[0] + last[2]) / 2,
                                        y=(last[1] + last[3]) / 2))
        assert panel.draft["appOrder"][0] == "offline"
        assert panel.draft["appOrder"].index("app-0") > 0
        panel._mixer_hide(SimpleNamespace(x=(last[0] + last[2]) / 2,
                                          y=(last[1] + last[3]) / 2))
        assert panel.draft["hiddenApps"]
        hidden_id = panel.draft["hiddenApps"][0]
        panel.hidden.selection_set(panel._hidden_ids.index(hidden_id))
        panel._show_app()
        assert hidden_id not in panel.draft["hiddenApps"]
        panel._mixer_hide(SimpleNamespace(x=(last[0] + last[2]) / 2,
                                          y=(last[1] + last[3]) / 2))

        # Library-to-pad drop displaces the previous assignment, while a pad
        # swap leaves the library and the total number of slots intact.
        panel.draft["padSlots"][1] = None
        panel._draw()
        assert panel._library_ids == ["horn"]
        panel.library.selection_set(0)
        panel._library_down(SimpleNamespace(y=panel.library.bbox(0)[1] + 2, x_root=0, y_root=0))
        pad = panel._slot_buttons[4]
        panel._library_up(SimpleNamespace(x_root=pad.winfo_rootx() + pad.winfo_width() // 2,
                                          y_root=pad.winfo_rooty() + pad.winfo_height() // 2))
        assert panel.draft["padSlots"][4] == "horn"
        assert len(panel.draft["padSlots"]) == 12
        panel.save()
        assert service.snapshot()["pages"]["soundboard"] == "left"
        assert service.snapshot()["appOrder"] == panel.draft["appOrder"]

        # A phone change arriving during a desktop draft must reject its Save.
        panel._clear_selected()
        service.update({"hiddenApps": []}, service.snapshot()["revision"])
        panel.update_snapshot(panel.admin.presentation_state())
        panel.save()
        assert panel.dirty
        assert "changed on the phone" in panel.status.cget("text")
        panel.discard()
        assert not panel.dirty
        assert panel.draft == service.snapshot()
    finally:
        root.destroy()
        # AudioEngine explicitly collects on its worker thread in another test.
        # Finalize Tk objects here on the owning thread first (Windows Tcl is
        # unsafe to finalize from that worker).
        del panel
        del root
        gc.collect()


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        exercise_hidden_layout_drag_save_and_remote_conflict(Path(directory))
