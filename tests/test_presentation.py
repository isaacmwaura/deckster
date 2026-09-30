"""Persisted phone presentation, independent of audio route configuration."""
from __future__ import annotations

import json

import pytest

from agent.presentation import PresentationService, RevisionConflict
from agent.state import AppState
from helpers import engine_client, hello, recv_until, run


class Library:
    def __init__(self, clips=(), layout="a"):
        self.clips = list(clips)
        self.layout = layout

    def snapshot(self):
        return {"clips": self.clips, "config": {"layout": self.layout}}


def test_restore_defaults_preserves_library_and_rejects_stale_draft(tmp_path):
    library = Library([{"id": "imported", "gain": .6}, {"id": "default-air-horn"}])
    service = PresentationService(tmp_path, library)
    saved = service.update({"appOrder": ["game"], "hiddenApps": ["chat"],
                            "padSlots": [None] * 12, "hideInteraction": "tap"}, 0)
    before = json.loads(json.dumps(library.snapshot()))
    with pytest.raises(RevisionConflict):
        service.restore_defaults(0)
    assert service.snapshot() == saved
    restored = service.restore_defaults(1)
    assert restored["appOrder"] == restored["hiddenApps"] == []
    assert restored["hideInteraction"] == "drag"
    assert restored["padSlots"][:2] == ["default-air-horn", "imported"]
    assert library.snapshot() == before
    assert PresentationService(tmp_path, library).snapshot() == restored


def test_legacy_layout_and_slots_migrate_once(tmp_path):
    clips = [{"id": f"sound-{i}"} for i in range(14)]
    library = Library(clips, "b")
    service = PresentationService(tmp_path, library)
    first = service.snapshot()
    assert first["pages"] == {"soundboard": "left", "devices": "right", "media": "bottom"}
    assert first["padSlots"] == [f"sound-{i}" for i in range(12)]
    library.layout = "a"
    restarted = PresentationService(tmp_path, library)
    assert restarted.snapshot() == first


def test_update_validates_revision_slots_and_page_positions(tmp_path):
    service = PresentationService(tmp_path, Library([{"id": "one"}, {"id": "two"}]))
    base = service.snapshot()
    saved = service.update({"padSlots": ["one", None] + [None] * 10,
                            "hiddenApps": ["ledkeeper2"],
                            "appOrder": ["ledkeeper2", "chrome"]}, 0)
    assert saved["revision"] == 1
    assert saved["hiddenApps"] == ["ledkeeper2"]
    with pytest.raises(RevisionConflict):
        service.update({"hiddenApps": []}, 0)
    with pytest.raises(ValueError, match="three different"):
        service.update({"pages": {"soundboard": "right", "devices": "right", "media": "bottom"}}, 1)
    with pytest.raises(ValueError, match="unique existing"):
        service.update({"padSlots": ["one", "one"] + [None] * 10}, 1)
    assert service.snapshot() == saved
    assert json.loads((tmp_path / "presentation.json").read_text()) == saved


def test_deleted_clip_clears_only_its_assignment(tmp_path):
    library = Library([{"id": "one"}, {"id": "two"}])
    service = PresentationService(tmp_path, library)
    library.clips.remove({"id": "one"})
    updated = service.remove_clip("one")
    assert updated["padSlots"][:2] == [None, "two"]
    assert updated["revision"] == 1
    assert service.remove_clip("one") is None


def test_websocket_presentation_broadcast_and_conflict(tmp_path):
    service = PresentationService(tmp_path, Library([{"id": "one"}]))

    async def body():
        async with engine_client(presentation=service) as (client, state, _controller):
            phone = await client.ws_connect("/ws")
            desktop = await client.ws_connect("/ws")
            await hello(phone)
            await hello(desktop)
            assert state.snapshot()["presentation"]["padSlots"][0] == "one"
            await phone.send_json({"t": "presentation_update", "baseRevision": 0,
                                   "changes": {"hiddenApps": ["ledkeeper2"]}})
            result = await recv_until(phone, "presentation")
            peer = await recv_until(desktop, "presentation")
            assert result == peer
            assert state.snapshot()["presentation"]["hiddenApps"] == ["ledkeeper2"]
            await desktop.send_json({"t": "presentation_update", "baseRevision": 0,
                                     "changes": {"appOrder": ["chrome"]}})
            conflict = await recv_until(desktop, "error")
            assert conflict["code"] == "presentation_conflict"
            assert conflict["presentation"]["revision"] == 1
            await phone.close()
            await desktop.close()

    run(body())


def test_missing_library_clip_is_sanitized_on_startup(tmp_path):
    library = Library([{"id": "one"}, {"id": "two"}])
    initial = PresentationService(tmp_path, library).snapshot()
    assert initial["padSlots"][:2] == ["one", "two"]
    library.clips = [{"id": "two"}]
    restarted = PresentationService(tmp_path, library).snapshot()
    assert restarted["padSlots"][:2] == [None, "two"]
    assert restarted["revision"] == 1


def test_corrupt_file_is_backed_up_and_app_starts(tmp_path):
    path = tmp_path / "presentation.json"
    path.write_text('{"revision": false, "broken": true}', encoding="utf-8")
    recovered = PresentationService(tmp_path, Library([{"id": "one"}])).snapshot()
    assert recovered["padSlots"][0] == "one"
    assert (tmp_path / "presentation.corrupt.json").read_text(encoding="utf-8") == '{"revision": false, "broken": true}'


def test_caller_changes_cannot_mutate_saved_state(tmp_path):
    service = PresentationService(tmp_path, Library())
    changes = {"appOrder": ["chrome"]}
    service.update(changes, 0)
    changes["appOrder"].append("steam")
    assert service.snapshot()["appOrder"] == ["chrome"]


def test_failed_save_rolls_back_memory_and_disk(tmp_path, monkeypatch):
    service = PresentationService(tmp_path, Library())
    original = service.snapshot()
    def fail():
        raise OSError("disk full")
    monkeypatch.setattr(service, "_save", fail)
    with pytest.raises(OSError, match="disk full"):
        service.update({"appOrder": ["chrome"]}, 0)
    assert service.snapshot() == original
    assert json.loads((tmp_path / "presentation.json").read_text()) == original


def test_stale_presentation_callback_cannot_overwrite_newer_state():
    state = AppState()
    fresh = {**state.presentation, "revision": 2, "hiddenApps": ["ledkeeper2"]}
    state.set_presentation(fresh)
    state.set_presentation({**fresh, "revision": 1, "hiddenApps": []})
    assert state.snapshot()["presentation"] == fresh


def test_mixer_moves_with_occupied_center_and_old_layout_survives(tmp_path):
    service = PresentationService(tmp_path, Library())
    original = service.snapshot()
    for side in ("left", "right", "top", "bottom"):
        available = [x for x in ("left", "right", "top", "bottom") if x != side]
        pages = {"mixer": side, "soundboard": "center", "devices": available[0], "media": available[1]}
        saved = service.update({"pages": pages, "hideInteraction": "tap"}, service.snapshot()["revision"])
        assert PresentationService(tmp_path, Library()).snapshot() == saved
    with pytest.raises(ValueError, match="center is empty"):
        service.update({"pages": {"mixer": "left", "soundboard": "top", "devices": "right", "media": "bottom"}}, saved["revision"])
    assert service.snapshot() == saved
    restored = service.update({"pages": original["pages"]}, saved["revision"])
    assert restored["pages"] == original["pages"]


def test_presentation_update_rejected_before_auth(tmp_path):
    class Auth:
        def requires_auth(self):
            return True
        async def handle_preauth(self, client, msg):
            return False

    service = PresentationService(tmp_path, Library())
    async def body():
        async with engine_client(presentation=service, authenticator=Auth()) as (client, _state, _controller):
            ws = await client.ws_connect("/ws")
            await ws.send_json({"t": "presentation_update", "baseRevision": 0,
                                "changes": {"hiddenApps": ["ledkeeper2"]}})
            assert (await recv_until(ws, "error"))["code"] == "unauth"
            await ws.close()
    run(body())
    assert service.snapshot()["revision"] == 0
