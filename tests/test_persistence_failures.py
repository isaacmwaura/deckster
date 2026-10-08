"""A failed durable write must not publish a phantom configuration."""
import pytest

from agent.macros.bindings import AppInputBindings
from agent.macros.registry import MacroRegistry


def fail():
    raise OSError("injected disk full")


def test_macro_add_remove_roll_back_on_persistence_failure(tmp_path, monkeypatch):
    registry = MacroRegistry(tmp_path / "macros.json")
    saved = registry.add("Mute", "ctrl+m")
    before = registry.list()
    monkeypatch.setattr(registry, "_save", fail)
    with pytest.raises(OSError):
        registry.add("Other", "ctrl+n")
    assert registry.list() == before
    with pytest.raises(OSError):
        registry.remove(saved["id"])
    assert registry.list() == before
    registry.get(saved["id"])["keys"] = "ctrl+z"
    registry.list()[0]["label"] = "changed externally"
    assert registry.list() == before
    assert MacroRegistry(tmp_path / "macros.json").list() == before


def test_binding_set_remove_roll_back_on_persistence_failure(tmp_path, monkeypatch):
    bindings = AppInputBindings(tmp_path / "bindings.json")
    bindings.set("discord", "ctrl+m")
    before = bindings.list()
    monkeypatch.setattr(bindings, "_save", fail)
    for app in ("discord", "new-app"):
        with pytest.raises(OSError):
            bindings.set(app, "ctrl+n")
        assert bindings.list() == before
    with pytest.raises(OSError):
        bindings.remove("discord")
    assert bindings.list() == before
    bindings.get("discord")["keys"] = "ctrl+z"
    assert bindings.list() == before
    assert AppInputBindings(tmp_path / "bindings.json").list() == before


def test_macro_id_collision_cannot_overwrite_existing_macro(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.macros.registry.secrets.token_hex", lambda count: "abcdef")
    registry = MacroRegistry(tmp_path / "macros.json")
    registry.add("Mute", "ctrl+m")
    before = registry.list()
    with pytest.raises(ValueError, match="collision"):
        registry.add("Mute", "ctrl+n")
    assert registry.list() == before
    assert MacroRegistry(tmp_path / "macros.json").list() == before
