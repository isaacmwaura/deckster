"""Plain-language route choices for the desktop's guided VB-CABLE setup."""
from __future__ import annotations

import re

ROUTE_KEYS = ("inputId", "voiceOutputId", "earsOutputId")


def cable_end(name: str, side: str) -> str | None:
    """Identify standard VB-CABLE / A / B etc. without guessing a 16ch pairing."""
    match = re.match(r"^(CABLE(?:-[A-Z])?)\s+" + side + r"(?:\s|\(|$)", name, re.I)
    return match.group(1).upper() if match else None


def cable_pairs(outputs: list[dict], inputs: list[dict]) -> list[tuple[dict, dict]]:
    pairs = []
    for output in outputs:
        key = cable_end(str(output.get("name", "")), "Input")
        if key:
            peer = next((d for d in inputs
                         if cable_end(str(d.get("name", "")), "Output") == key), None)
            if peer:
                pairs.append((output, peer))
    return sorted(pairs, key=lambda pair: (cable_end(pair[0]["name"], "Input") != "CABLE",
                                            pair[0]["name"]))


def is_virtual(device: dict) -> bool:
    return bool(re.search(r"cable|voicemeeter|virtual|sonar|broadcast|loopback",
                          str(device.get("name", "")), re.I))


def recommend_route(snapshot: dict) -> dict[str, str]:
    saved = snapshot.get("config", {})
    mics = [d for d in snapshot.get("inputs", []) if not is_virtual(d)]
    pairs = cable_pairs(snapshot.get("outputs", []), snapshot.get("inputs", []))
    if not mics:
        raise ValueError("Connect a physical microphone to this PC first.")
    if not pairs:
        raise ValueError("No complete virtual cable found. Install VB-CABLE, then return here.")
    mic = next((d for d in mics if d["id"] == saved.get("inputId")), None)
    mic = mic or next((d for d in mics if d.get("isDefault")), mics[0])
    cable = next((p[0] for p in pairs if p[0]["id"] == saved.get("voiceOutputId")), pairs[0][0])
    monitors = [d for d in snapshot.get("outputs", []) if not is_virtual(d)]
    ears = next((d["id"] for d in monitors if d["id"] == saved.get("earsOutputId")), "")
    return {"inputId": mic["id"], "voiceOutputId": cable["id"], "earsOutputId": ears}


def route_issue(config: dict, snapshot: dict) -> str:
    mic = next((d for d in snapshot.get("inputs", []) if d["id"] == config.get("inputId")), None)
    if mic is None:
        return "Choose your microphone in box 1."
    if is_virtual(mic):
        return "Box 1 needs your physical microphone. The cable's Output belongs in your call/game."
    pairs = cable_pairs(snapshot.get("outputs", []), snapshot.get("inputs", []))
    if not any(p[0]["id"] == config.get("voiceOutputId") for p in pairs):
        return "Box 2 needs a virtual cable, not a speaker output. Use recommended setup to select one."
    if config.get("earsOutputId"):
        monitor = next((d for d in snapshot.get("outputs", [])
                        if d["id"] == config["earsOutputId"]), None)
        if monitor is None or is_virtual(monitor):
            return "In box 3, choose headphones/speakers or turn your own clip playback off."
    return ""


def receiving_microphone(config: dict, snapshot: dict) -> str:
    return next((p[1]["name"] for p in cable_pairs(snapshot.get("outputs", []), snapshot.get("inputs", []))
                 if p[0]["id"] == config.get("voiceOutputId")), "Choose a virtual cable in box 2 first")
