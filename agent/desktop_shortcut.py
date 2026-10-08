"""Keep one Desktop shortcut pointing to the running packaged Deckster build."""
from pathlib import Path
import os


def update_shortcut(executable: Path, shell=None) -> Path:
    # Preserve the launch path. Resolving reparse points inside an MSIX host can
    # turn the stable installed path into that host's private LocalCache path.
    executable = executable.absolute()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    if shell is None:
        from comtypes.client import CreateObject
        shell = CreateObject("WScript.Shell", dynamic=True)
    desktop = Path(str(shell.SpecialFolders.Item("Desktop"))).resolve(strict=True)
    canonical = desktop / "Deckster.lnk"
    temporary = desktop / "Deckster-update.lnk"
    link = shell.CreateShortcut(str(temporary))
    link.TargetPath = str(executable)
    link.Arguments = ""
    link.WorkingDirectory = str(executable.parent)
    link.IconLocation = str(executable) + ",0"
    link.Description = "Deckster — latest installed version"
    link.Save()
    check = shell.CreateShortcut(str(temporary))
    if Path(str(check.TargetPath)).absolute() != executable:
        raise OSError("Desktop shortcut target did not verify")
    temporary.replace(canonical)
    directories = {desktop}
    for relative in ("Desktop", "OneDrive/Desktop"):
        candidate = Path(os.environ.get("USERPROFILE", str(desktop.parent))) / relative
        if candidate.is_dir():
            directories.add(candidate.resolve())
    for directory in directories:
        for old in directory.glob("Deckster*.lnk"):
            if old == canonical:
                continue
            target = Path(str(shell.CreateShortcut(str(old)).TargetPath))
            # Remove only shortcuts to identifiable Deckster executables.
            if target.name.lower() == "deckster.exe" or (
                    target.name.lower().startswith("deckster-v") and target.suffix.lower() == ".exe"):
                old.unlink()
    return canonical
