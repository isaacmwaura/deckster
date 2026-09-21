# PyInstaller spec for Deckster — build with:  pyinstaller build/streamcontrol.spec
#
# Notes:
# - comtypes/pycaw generate interface wrappers at runtime; collect_submodules pulls
#   them in so the frozen exe doesn't miss COM interfaces (the classic freeze bug).
# - web/ is bundled as data and resolved at runtime via config.resource_root()
#   (sys._MEIPASS when frozen). bin/ (bundled adb) is included only if non-empty.
# - windowed build (no console); logs still go to %LOCALAPPDATA%\StreamControl\agent.log.
import os
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
_bin = os.path.join(ROOT, "bin")
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
