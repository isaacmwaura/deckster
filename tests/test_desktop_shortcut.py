"""A new build replaces the link; only older Deckster links are removed."""
import json
from pathlib import Path
from types import SimpleNamespace

from agent.desktop_shortcut import update_shortcut


def test_latest_shortcut_replaces_older_versions_and_preserves_unrelated(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    exe = tmp_path / "Deckster-v0.6.5.exe"
    exe.write_bytes(b"MZ")
    class Link:
        def __init__(self, path):
            self.path = Path(path)
            self.TargetPath = json.loads(self.path.read_text())["target"] if self.path.exists() else ""
        def Save(self):
            self.path.write_text(json.dumps({"target": self.TargetPath}))
    shell = SimpleNamespace(SpecialFolders=SimpleNamespace(Item=lambda name: str(desktop)), CreateShortcut=Link)
    for name, target in (("Deckster.lnk", "Deckster-v0.6.4.exe"),
                         ("Deckster-v0.6.3.lnk", "Deckster-v0.6.3.exe"),
                         ("Deckster notes.lnk", "notes.txt")):
        link = Link(desktop / name)
        link.TargetPath = str(tmp_path / target)
        link.Save()
    result = update_shortcut(exe, shell)
    assert result == desktop / "Deckster.lnk"
    assert Path(Link(result).TargetPath) == exe
    assert not (desktop / "Deckster-v0.6.3.lnk").exists()
    assert not (desktop / "Deckster-update.lnk").exists()
    assert (desktop / "Deckster notes.lnk").exists()
