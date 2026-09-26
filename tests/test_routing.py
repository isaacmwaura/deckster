"""Recommendations must connect the right cable ends without changing live settings."""
import copy

import pytest

from agent.routing import cable_pairs, recommend_route, route_issue, receiving_microphone


def devices():
    return {
        "inputs": [{"id": "mic", "name": "Microphone (Realtek)", "isDefault": True},
                   {"id": "exit", "name": "CABLE Output (VB-Audio Virtual Cable)"},
                   {"id": "exit-a", "name": "CABLE-A Output (VB-Audio Cable A)"}],
        "outputs": [{"id": "speakers", "name": "Speakers (Realtek)"},
                    {"id": "entry-a", "name": "CABLE-A Input (VB-Audio Cable A)"},
                    {"id": "entry", "name": "CABLE Input (VB-Audio Virtual Cable)"},
                    {"id": "multi", "name": "CABLE In 16ch (VB-Audio Virtual Cable)"}],
        "config": {"inputId": "mic", "voiceOutputId": "speakers", "earsOutputId": "speakers"},
        "clips": [], "runtime": "setup_required", "error": "",
    }


def test_recommendation_repairs_physical_voice_preview_without_mutating_saved_route():
    snapshot = devices()
    original = copy.deepcopy(snapshot)
    recommendation = recommend_route(snapshot)
    assert recommendation == {"inputId": "mic", "voiceOutputId": "entry", "earsOutputId": "speakers"}
    assert snapshot == original
    assert not route_issue(recommendation, snapshot)
    assert receiving_microphone(recommendation, snapshot) == "CABLE Output (VB-Audio Virtual Cable)"
    assert route_issue(snapshot["config"], snapshot)


def test_matching_named_cables_preserves_correct_saved_pair():
    snapshot = devices()
    snapshot["config"]["voiceOutputId"] = "entry-a"
    result = recommend_route(snapshot)
    assert result["voiceOutputId"] == "entry-a"
    assert receiving_microphone(result, snapshot) == "CABLE-A Output (VB-Audio Cable A)"
    assert len(cable_pairs(snapshot["outputs"], snapshot["inputs"])) == 2


def test_missing_cable_or_physical_mic_does_not_guess_a_speaker_route():
    snapshot = devices()
    snapshot["outputs"] = snapshot["outputs"][:1]
    with pytest.raises(ValueError, match="virtual cable"):
        recommend_route(snapshot)
    snapshot = devices()
    snapshot["inputs"] = snapshot["inputs"][1:]
    with pytest.raises(ValueError, match="physical microphone"):
        recommend_route(snapshot)


def test_unplugged_monitor_blocks_connect_and_virtual_mic_cannot_feed_itself():
    snapshot = devices()
    result = recommend_route(snapshot)
    snapshot["outputs"] = snapshot["outputs"][1:]
    assert "box 3" in route_issue(result, snapshot)
    result["earsOutputId"] = ""
    result["inputId"] = "exit"
    assert "physical microphone" in route_issue(result, snapshot)


def test_legacy_physical_voice_is_not_auto_started(tmp_path):
    from agent.soundboard import SoundboardService
    service = SoundboardService(tmp_path, renderer_factory=lambda: pytest.fail("must not open audio"),
                                defaults_root=tmp_path / "no-pack")
    snapshot = devices()
    service._data["config"].update(snapshot["config"])
    service.ensure_started(snapshot["outputs"], snapshot["inputs"])
    assert service.snapshot()["runtime"] == "setup_required"
    assert "physical output" in service.snapshot()["error"]
    assert service.snapshot()["config"]["voiceOutputId"] == "speakers"


def test_guided_panel_recommendation_is_a_draft_until_connect():
    import tkinter as tk
    from agent.routing_panel import RoutingPanel
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk unavailable")
    root.withdraw()  # Never display or activate a window during verification.
    class Admin:
        def __init__(self):
            self.snapshot = devices()
            self.applied = []
        def soundboard_state(self): return copy.deepcopy(self.snapshot)
        def configure_soundboard(self, config):
            self.applied.append(config)
            self.snapshot["config"] = config
            self.snapshot["runtime"] = "ready"
            return self.soundboard_state()
    try:
        admin = Admin()
        panel = RoutingPanel(root, admin)
        panel.recommend()
        assert admin.applied == [] and panel.dirty
        assert panel.app_mic.get().startswith("CABLE Output")
        assert str(panel.test_others["state"]) == "disabled"
        panel.update_snapshot(admin.soundboard_state())
        assert panel.draft["voiceOutputId"] == "entry"  # polling must preserve edits
        panel.connect()
        assert admin.applied[0]["voiceOutputId"] == "entry"
        assert not panel.dirty and str(panel.test_others["state"]) == "normal"
        assert root.state() == "withdrawn"
    finally:
        root.destroy()
