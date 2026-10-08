"""Headless, isolated smoke check of a built Deckster executable.

This never uses the installed user's settings or sends audio/media commands.
Example: python tools/verify_packaged_review.py dist/Deckster-v0.6.0.exe
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

import aiohttp


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _check(port: int, token: str, expected_version: str,
                 expected_clips: int, expected_slots: int,
                 process: subprocess.Popen, require_command_protocol: bool = False) -> dict:
    base = f"http://127.0.0.1:{port}"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        deadline = time.monotonic() + 45
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Packaged Deckster exited early ({process.returncode})")
            try:
                async with session.get(base + "/health") as response:
                    if response.status == 200 and (await response.json()).get("ok") is True:
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("Packaged Deckster did not pass loopback health check")
            await asyncio.sleep(.25)

        async with session.get(base + "/admin/api/state") as response:
            response.raise_for_status()
            admin = await response.json()
        if admin.get("version") != expected_version:
            raise AssertionError(f"Version {admin.get('version')!r} != {expected_version!r}")
        if admin.get("mode") != "loopback":
            raise AssertionError("Packaged app did not use loopback mode")
        desktop_url = admin.get("desktopUrl", "")
        if not desktop_url.startswith("http://127.0.0.1:"):
            raise AssertionError("Dedicated desktop origin was not started")
        desktop_base = desktop_url.removesuffix("/admin")
        async with session.get(desktop_base + "/admin/api/workspace") as response:
            response.raise_for_status()
            workspace = await response.json()
            if workspace["admin"]["version"] != expected_version:
                raise AssertionError("Desktop workspace version mismatch")
        async with session.get(desktop_base + "/ws") as response:
            if response.status != 404:
                raise AssertionError("Desktop listener must not expose a phone socket")
        async with session.post(desktop_base + "/admin/api/mode", json={"mode": "lan"},
                                headers={"Origin": "https://untrusted.example"}) as response:
            if response.status != 403:
                raise AssertionError("Desktop listener accepted a foreign origin")

        async with session.ws_connect(base.replace("http:", "ws:") + "/ws") as ws:
            await ws.send_json({"t": "hello", "token": token, "deviceName": "Packaged review"})
            snapshot = None
            for _ in range(10):
                message = await ws.receive_json(timeout=5)
                if message.get("t") == "snapshot":
                    snapshot = message
                    break
                if message.get("t") in {"need_pair", "error"}:
                    raise AssertionError(f"Isolated WebSocket authentication failed: {message.get('t')}")
            if snapshot is None:
                raise AssertionError("Authenticated WebSocket snapshot was not received")
            if require_command_protocol:
                if not {"command-results", "owner-sequence"}.issubset(snapshot.get("capabilities", [])):
                    raise AssertionError("Rebuilt command protocol is absent from the package")
                epoch = snapshot.get("serverEpoch")
                if not epoch:
                    raise AssertionError("Package did not announce a server epoch")
                await ws.send_json({"t": "set_volume", "target": {"kind": "mic"}, "level": .37,
                                    "commandId": "package-review-volume", "clientSeq": 1, "serverEpoch": epoch})
                accepted = False
                while True:
                    message = await ws.receive_json(timeout=5)
                    if message.get("commandId") != "package-review-volume":
                        continue
                    accepted |= message.get("status") == "accepted"
                    if message.get("status") == "applied":
                        if not accepted or message.get("ownerSeq", 0) <= 0 or message.get("observation", {}).get("level") != .37:
                            raise AssertionError("Packaged mock write/readback did not reconcile")
                        break
                    if message.get("status") in {"failed", "outcome_unknown"}:
                        raise AssertionError("Packaged isolated control failed")

        if require_command_protocol:
            async with session.get(desktop_base + "/ready") as response:
                if response.status != 200 or not (await response.json()).get("ready"):
                    raise AssertionError("Packaged engine/connection is not ready")
            async with session.get(desktop_base + "/admin/api/diagnostics") as response:
                response.raise_for_status()
                if "audio" not in await response.json():
                    raise AssertionError("Packaged audio diagnostics missing")
            async with session.get(base + "/ready", headers={"Host": "untrusted.example"}) as response:
                if response.status != 403:
                    raise AssertionError("Packaged diagnostics accepted a foreign Host")

        clips = snapshot.get("soundboard", {}).get("clips", [])
        slots = snapshot.get("presentation", {}).get("padSlots", [])
        if len(clips) != expected_clips:
            raise AssertionError(f"Soundboard library has {len(clips)} clips, expected {expected_clips}")
        if len(slots) != expected_slots:
            raise AssertionError(f"Phone layout has {len(slots)} slots, expected {expected_slots}")
        return {"version": admin["version"], "mode": admin["mode"], "desktopUrl": desktop_url,
                "libraryClips": len(clips), "phoneSlots": len(slots),
                "authenticatedWebSocket": True,
                "commandProtocolReadinessAndDiagnostics": require_command_protocol}


def _stop_own_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--output-root", type=Path,
                        help="isolated results directory (default: release/verify-vVERSION)")
    parser.add_argument("--expected-version", default="0.6.0")
    parser.add_argument("--expected-clips", type=int, default=16)
    parser.add_argument("--expected-slots", type=int, default=12)
    parser.add_argument("--require-command-protocol", action="store_true",
                        help="verify v0.7+ IDs/readback/readiness using only the isolated mock backend")
    args = parser.parse_args()
    exe = args.exe.resolve(strict=True)
    if not exe.is_file() or exe.suffix.lower() != ".exe":
        parser.error("exe must be an existing Windows executable")
    with exe.open("rb") as stream:
        signature = stream.read(2)
    if signature != b"MZ":
        parser.error("exe has no Windows executable signature")

    output_root = args.output_root or Path("release") / f"verify-v{args.expected_version}"
    run_root = output_root.resolve() / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    data_root = run_root / "localappdata"
    state = data_root / "StreamControl"
    state.mkdir(parents=True)
    token = secrets.token_urlsafe(32)
    salt = secrets.token_hex(16)
    (state / "settings.json").write_text(json.dumps({
        "mode": "loopback", "secure": False, "token_salt": salt,
    }), encoding="utf-8")
    (state / "allowlist.json").write_text(json.dumps({
        "packaged-review": {
            "name": "Packaged review",
            "token_hash": hashlib.sha256(f"{salt}:{token}".encode()).hexdigest(),
            "paired_at": int(time.time()),
        },
    }), encoding="utf-8")
    port = _free_port()
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(data_root)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [str(exe), "--mock", "--no-tray", "--mode", "loopback", "--port", str(port)],
        cwd=str(exe.parent), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        result = asyncio.run(_check(port, token, args.expected_version,
                                    args.expected_clips, args.expected_slots, process,
                                    args.require_command_protocol))
        log_path = state / "agent.log"
        from PyInstaller.archive.readers import CArchiveReader
        archive = CArchiveReader(str(exe))
        contents = {name.replace('\\', '/') for name in archive.toc}
        required = {'bin/adb/adb.exe', 'bin/adb/AdbWinApi.dll', 'bin/adb/AdbWinUsbApi.dll', 'bin/adb/NOTICE.txt'}
        if not required.issubset(contents):
            raise AssertionError("Standalone USB runtime incomplete: " + str(required - contents))
        if not any(name.lower().startswith('python3') and name.lower().endswith('.dll') for name in contents):
            raise AssertionError("Bundled Python runtime missing")
        result["bundledPythonAndUsbRuntimeVerified"] = True
        if log_path.exists() and "adb watcher started" in log_path.read_text(encoding="utf-8"):
            raise AssertionError("Mock review must not start the live USB watcher")
        result["liveUsbWatcherDisabled"] = True
        (run_root / "result.json").write_text(json.dumps(result, indent=2) + "\n",
                                               encoding="utf-8")
        print(f"Packaged review passed: {result}")
        print(f"Isolated output: {run_root}")
    finally:
        _stop_own_process_tree(process)


if __name__ == "__main__":
    main()
