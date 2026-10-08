# PyInstaller spec for Deckster — build with:  pyinstaller build/streamcontrol.spec
#
# Notes:
# - comtypes/pycaw generate interface wrappers at runtime; collect_submodules pulls
#   them in so the frozen exe doesn't miss COM interfaces (the classic freeze bug).
# - web/ is bundled as data and resolved at runtime via config.resource_root()
#   (sys._MEIPASS when frozen). bin/ (bundled adb) is included only if non-empty.
# - windowed build (no console); logs still go to %LOCALAPPDATA%\StreamControl\agent.log.
import os
import json
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = os.path.abspath(os.getcwd())
version_ns = {}
with open(os.path.join(ROOT, "agent", "__init__.py"), encoding="utf-8") as version_file:
    exec(version_file.read(), version_ns)
VERSION = version_ns["__version__"]

hiddenimports = []
for pkg in ("comtypes", "pycaw", "aiohttp", "pystray", "PIL", "qrcode", "sounddevice", "soundfile"):
    hiddenimports += collect_submodules(pkg)

datas = [
    (os.path.join(ROOT, "web"), "web"),
    (os.path.join(ROOT, "assets", "default-sounds"), os.path.join("assets", "default-sounds")),
]
# The recordings are user-provided local content, excluded from Git. A clean
# clone builds with starter sounds; a fully staged local pack is bundled too.
_meme_root = os.path.join(ROOT, "assets", "meme-pack")
_meme_audio = os.path.join(_meme_root, "audio")
if os.path.isdir(_meme_audio) and any(os.scandir(_meme_audio)):
    with open(os.path.join(_meme_root, "manifest.json"), encoding="utf-8") as meme_file:
        _meme_manifest = json.load(meme_file)
    if not all(os.path.isfile(os.path.join(_meme_audio, clip["file"]))
               for clip in _meme_manifest["clips"]):
        raise RuntimeError("Local meme pack is incomplete. Complete the locally supplied recordings before building.")
    datas += [
        (os.path.join(_meme_root, "manifest.json"), os.path.join("assets", "meme-pack")),
        (_meme_audio, os.path.join("assets", "meme-pack", "audio")),
    ]
_bin = os.path.join(ROOT, "bin")
if not all(os.path.isfile(os.path.join(_bin, "adb", name)) for name in
           ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt")):
    raise RuntimeError("Standalone USB builds need Android platform-tools staged in bin/adb (adb.exe, its DLLs and NOTICE.txt). See README build instructions.")
if os.path.isdir(_bin) and any(os.scandir(_bin)):
    datas.append((_bin, "bin"))
datas += collect_data_files("comtypes")

a = Analysis(
    [os.path.join(ROOT, "build", "entry.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "playwright"],   # tkinter IS needed (the control-panel window)
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    # Every local build has an immediately identifiable, versioned filename.
    name=f"Deckster-v{VERSION}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # windowed; tray app
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "build", "deckster.ico"),
)
