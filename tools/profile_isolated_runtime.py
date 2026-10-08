"""Measure an isolated source agent's control cost, without live audio or USB.

Uses the real engine thread with the mock backend. These measurements describe
PC control overhead only; they do not establish audio deadlines, Android energy,
renderer cost, or gaming impact. Requires developer-only psutil.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import statistics
import subprocess
import sys
import time

import aiohttp
import psutil


def percentile(values, p):
    return sorted(values)[min(len(values) - 1, int((len(values) - 1) * p))] if values else 0


async def profile(args, process, port, token):
    base = f"http://127.0.0.1:{port}"
    proc = psutil.Process(process.pid)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Isolated agent exited before health became available")
            try:
                async with session.get(base + "/health") as response:
                    if response.status == 200:
                        break
            except (aiohttp.ClientError, TimeoutError):
                pass
            await asyncio.sleep(.1)
        else:
            raise TimeoutError("Isolated startup did not finish")
        # Let imports/default pack and the first engine poll finish before sampling.
        await asyncio.sleep(2)
        results = []
        for connected in (False, True):
            ws = await session.ws_connect(base.replace("http", "ws") + "/ws") if connected else None
            if ws is not None:
                await ws.send_json({"t": "hello", "token": token, "deviceName": "Isolated profile"})
                while (await ws.receive_json())["t"] != "snapshot":
                    pass
            proc.cpu_percent()
            cpu, rss, private, latencies = [], [], [], []
            state_messages, wire_bytes, pings = 0, 0, 0
            started = time.monotonic()
            cpu_started = proc.cpu_times()
            next_ping = started
            pending_ping = None
            while time.monotonic() - started < args.seconds:
                if ws is not None:
                    if pending_ping is None and time.monotonic() >= next_ping:
                        pending_ping = time.monotonic()
                        await ws.send_json({"t": "ping"})
                    try:
                        message = await ws.receive(timeout=.1)
                        if message.type == aiohttp.WSMsgType.TEXT:
                            wire_bytes += len(message.data.encode())
                            data = json.loads(message.data)
                            if data.get("t") == "pong" and pending_ping is not None:
                                latencies.append((time.monotonic() - pending_ping) * 1000)
                                pings += 1
                                pending_ping = None
                                next_ping = time.monotonic() + .3
                            elif data.get("t") in {"snapshot", "state", "update", "meters", "telemetry"}:
                                state_messages += 1
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(.1)
                cpu.append(proc.cpu_percent())
                info = proc.memory_info()
                rss.append(info.rss)
                private.append(getattr(info, "private", getattr(info, "pagefile", info.rss)))
            duration = time.monotonic() - started
            cpu_finished = proc.cpu_times()
            cpu_seconds = cpu_finished.user + cpu_finished.system - cpu_started.user - cpu_started.system
            if ws is not None:
                await ws.close()
            results.append({"scenario": "connected idle phone" if connected else "disconnected idle",
                "seconds": round(duration, 3), "samples": len(cpu),
                "cpuOneCorePercentMean": round(100 * cpu_seconds / duration, 3),
                "cpuOneCorePercentP95": round(percentile(cpu, .95), 3),
                "residentMiBMean": round(statistics.mean(rss) / 2**20, 3),
                "privateMiBPeak": round(max(private) / 2**20, 3),
                "stateMessages": state_messages, "wireBytes": wire_bytes, "pings": pings,
                "pingMsP50": round(percentile(latencies, .5), 3),
                "pingMsP95": round(percentile(latencies, .95), 3)})
        async with session.get(base + "/ready") as response:
            readiness = await response.json() if response.status != 404 else None
        return {"scenarios": results, "readiness": readiness}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error("seconds must be between 1 and 300 per scenario")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run_root = args.output.parent / (args.output.stem + "-isolated-state")
    data = run_root / "StreamControl"
    data.mkdir(parents=True, exist_ok=True)
    token, salt = secrets.token_urlsafe(32), secrets.token_hex(16)
    (data / "settings.json").write_text(json.dumps({"mode": "loopback", "secure": False, "token_salt": salt}))
    (data / "allowlist.json").write_text(json.dumps({"profile": {"name": "Isolated profile",
        "token_hash": hashlib.sha256(f"{salt}:{token}".encode()).hexdigest(), "paired_at": int(time.time())}}))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, LOCALAPPDATA=str(run_root.resolve()))
    process = subprocess.Popen([sys.executable, "-m", "agent.main", "--mock", "--no-tray", "--port", str(port)],
        cwd=args.source_root.resolve(), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        result = asyncio.run(profile(args, process, port, token))
        result.update({"sourceRoot": str(args.source_root.resolve()), "backend": "mock",
            "logicalCpus": psutil.cpu_count(), "totalMemoryGiB": round(psutil.virtual_memory().total / 2**30, 2),
            "scope": "Agent process only; no live audio, browser renderer, physical phone, USB watcher or game."})
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            else:
                process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
