"""Collection installation must preserve the existing library and pad layout."""
import copy
import hashlib
import json
import wave
from pathlib import Path

import pytest

from agent.presentation import PresentationService
from agent.soundboard import SoundboardService

ROOT = Path(__file__).resolve().parents[1]


def pack_fixture(tmp_path):
    pack = tmp_path / "pack"
    (pack / "audio").mkdir(parents=True)
    with wave.open(str(pack / "audio" / "clip.wav"), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 800)
    entry = {"key": "funny", "file": "clip.wav", "label": "Funny",
             "artwork": "funny.webp", "tags": ["reaction"],
             "sha256": hashlib.sha256((pack / "audio" / "clip.wav").read_bytes()).hexdigest()}
    manifest = {"id": "test-pack", "label": "Meme pack", "version": 1, "clips": [entry]}
    (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack, manifest


def test_pack_preserves_layout_routes_edits_and_removed_clips(tmp_path):
    pack, _ = pack_fixture(tmp_path)
    root = tmp_path / "data"
    service = SoundboardService(root)
    presentation = PresentationService(root, service)
    layout = presentation.path.read_bytes()
    config = service.snapshot()["config"]
    assert service.install_pack(pack) == 1
    assert presentation.path.read_bytes() == layout
    assert service.snapshot()["config"] == config
    clip = next(c for c in service.snapshot()["clips"] if c.get("packId"))
    assert clip["collection"] == "Meme pack"
    assert clip["artwork"] == "/static/meme-art/funny.webp"
    assert clip["duration"] == .1
    service.update_clip(clip["id"], label="My reaction", gain=.31)
    assert service.install_pack(pack) == 0
    assert next(c for c in service.snapshot()["clips"] if c["id"] == clip["id"])["label"] == "My reaction"
    service.remove_clip(clip["id"])
    restarted = SoundboardService(root)
    assert restarted.install_pack(pack) == 0
    assert clip["id"] not in {c["id"] for c in restarted.snapshot()["clips"]}
    assert PresentationService(root, restarted).snapshot() == presentation.snapshot()


def test_pack_failed_save_rolls_back_audio_and_library(tmp_path, monkeypatch):
    pack, _ = pack_fixture(tmp_path)
    service = SoundboardService(tmp_path / "data")
    before = copy.deepcopy(service.snapshot())
    persisted = service._path.read_bytes()
    files = set(service.clips_dir.iterdir())
    monkeypatch.setattr(service, "_save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        service.install_pack(pack)
    assert service.snapshot() == before
    assert service._path.read_bytes() == persisted
    assert set(service.clips_dir.iterdir()) == files


@pytest.mark.parametrize("damage", ["hash", "path", "duplicate", "label", "artwork"])
def test_invalid_pack_cannot_partially_install(tmp_path, damage):
    pack, manifest = pack_fixture(tmp_path)
    entry = manifest["clips"][0]
    if damage == "hash":
        entry["sha256"] = "0" * 64
    elif damage == "path":
        entry["file"] = "../clip.wav"
    elif damage == "duplicate":
        manifest["clips"].append(dict(entry))
    elif damage == "label":
        manifest["clips"].append(dict(entry, key="other", artwork="other.webp", label=None))
    else:
        entry["artwork"] = "https://example.com/tracker.webp"
    (pack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    service = SoundboardService(tmp_path / "data")
    before = service.snapshot()
    with pytest.raises(ValueError):
        service.install_pack(pack)
    assert service.snapshot() == before


@pytest.mark.parametrize("desktop", [False, True])
async def test_artwork_has_explicit_webp_type_on_both_listeners(desktop, tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from agent.server import create_app, create_desktop_app
    from agent.state import AppState
    from PIL import Image
    from agent import server
    web = tmp_path / 'web'
    (web / 'meme-art').mkdir(parents=True)
    Image.new('RGB', (16, 16), '#336699').save(web / 'meme-art' / 'fixture.webp', 'WEBP')
    monkeypatch.setattr(server, 'WEB_DIR', web)
    app = create_desktop_app(AppState(), None, None) if desktop else create_app(AppState())
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        response = await client.get("/static/meme-art/fixture.webp")
        assert response.status == 200
        assert response.content_type == "image/webp"
        assert (await response.read())[:4] == b"RIFF"
        assert (await client.get("/static/meme-art/missing.webp")).status == 404
