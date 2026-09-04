"""P2: tokens, allow-list, pairing, adb parsing, and the auth handshake over ws."""
import json

import pytest

from agent.security.allowlist import AllowList
from agent.security.auth import DeviceAuthenticator
from agent.security.pairing import PairingManager
from agent.security.tokens import hash_token, new_token, verify_token
from agent.transport import adb

from helpers import engine_client, recv, run


# ---- tokens ---------------------------------------------------------------
def test_token_hash_roundtrip():
    salt = "s@lt"
    tok = new_token()
    h = hash_token(tok, salt)
    assert verify_token(tok, h, salt)
    assert not verify_token("wrong", h, salt)
    assert not verify_token(tok, hash_token(tok, "other-salt"), salt)


# ---- allow-list -----------------------------------------------------------
def test_allowlist_add_find_revoke(tmp_path):
    salt = "abc"
    path = tmp_path / "allowlist.json"
    al = AllowList(path, salt)
    tok = new_token()
    al.add("dev-1", "Phone", hash_token(tok, salt))

    assert al.find_by_token(tok) == "dev-1"
    assert al.find_by_token("nope") is None
    assert [d["id"] for d in al.list_devices()] == ["dev-1"]

    # persistence: a fresh instance reads the same file
    al2 = AllowList(path, salt)
    assert al2.find_by_token(tok) == "dev-1"

    assert al2.revoke("dev-1") is True
    assert al2.find_by_token(tok) is None
    assert AllowList(path, salt).find_by_token(tok) is None


# ---- pairing --------------------------------------------------------------
def test_pairing_correct_and_single_use():
    pm = PairingManager(ttl_s=60, max_attempts=5)
    code = pm.current_code()
    assert pm.verify(code) is True
    # single use: same code no longer valid (a new one was generated)
    assert pm.verify(code) is False


def test_pairing_wrong_code():
    pm = PairingManager(ttl_s=60, max_attempts=5)
    pm.current_code()
    assert pm.verify("000000" if pm.current_code() != "000000" else "111111") in (False,)


def test_pairing_rate_limit_burns_code():
    pm = PairingManager(ttl_s=60, max_attempts=3)
    code = pm.current_code()
    for _ in range(3):
        pm.verify("bad")  # exhaust attempts
    # next attempt (even with the right code) is refused and the code is burned
    assert pm.verify(code) is False


def test_pairing_expiry():
    pm = PairingManager(ttl_s=0, max_attempts=5)  # already expired
    code = pm.current_code()
    assert pm.verify(code) is False


# ---- adb parsing ----------------------------------------------------------
def test_parse_devices_filters_states():
    out = (
        "List of devices attached\n"
        "ABC123\tdevice\n"
        "DEF456\tunauthorized\n"
        "GHI789\toffline\n"
        "JKL012\tdevice\n"
    )
    assert adb.parse_devices(out) == ["ABC123", "JKL012"]


def test_reverse_args():
    assert adb.reverse_args("adb", 8765) == ["adb", "reverse", "tcp:8765", "tcp:8765"]


def test_adb_unavailable_is_graceful(monkeypatch):
    # Simulate adb missing regardless of whether the host has it installed.
    monkeypatch.setattr(adb, "locate_adb", lambda: None)
    a = adb.AdbReverse(8765)  # no adb_path -> auto-locate, now returns None
    assert a.available() is False
    assert a.devices() == []
    assert a.apply_reverse() is False
    a.start_watcher()  # no-op, must not raise


# ---- auth handshake over the WebSocket ------------------------------------
# Run under asyncio.run with an inline engine (see tests/helpers.py) so the auth
# gate is exercised end to end without the harness deadlock.
def _make_auth(tmp_path, salt="test-salt"):
    allowlist = AllowList(tmp_path / "allowlist.json", salt)
    pairing = PairingManager(ttl_s=60, max_attempts=5)
    return DeviceAuthenticator(allowlist, pairing, salt), pairing


def test_command_rejected_before_auth(tmp_path):
    async def body():
        auth, _pairing = _make_auth(tmp_path)
        async with engine_client(authenticator=auth) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await ws.send_str(json.dumps({
                "t": "set_volume", "target": {"kind": "mic"}, "level": 0.5,
            }))
            msg = await recv(ws)
            assert msg["t"] == "error" and msg["code"] == "unauth"
            await ws.close()
    run(body())


def test_hello_without_token_prompts_pairing(tmp_path):
    async def body():
        auth, _pairing = _make_auth(tmp_path)
        async with engine_client(authenticator=auth) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await ws.send_str(json.dumps({"t": "hello", "token": ""}))
            msg = await recv(ws)
            assert msg["t"] == "need_pair"
            await ws.close()
    run(body())


def test_pair_then_use_token_on_new_connection(tmp_path):
    async def body():
        auth, pairing = _make_auth(tmp_path)
        async with engine_client(authenticator=auth) as (client, _s, _c):
            # 1) pair with the current code -> receive a token
            ws = await client.ws_connect("/ws")
            await ws.send_str(json.dumps({"t": "hello", "token": ""}))
            assert (await recv(ws))["t"] == "need_pair"
            code = pairing.current_code()
            await ws.send_str(json.dumps({
                "t": "pair", "code": code, "device": {"id": "dev-9", "name": "TestPhone"},
            }))
            ok = await recv(ws)
            assert ok["t"] == "pair_ok" and ok["token"]
            token = ok["token"]
            assert (await recv(ws))["t"] == "snapshot"  # authed -> snapshot follows
            await ws.close()

            # 2) a brand-new connection authenticates with the stored token
            ws2 = await client.ws_connect("/ws")
            await ws2.send_str(json.dumps({"t": "hello", "token": token}))
            assert (await recv(ws2))["t"] == "snapshot"
            await ws2.send_str(json.dumps({
                "t": "set_mute", "target": {"kind": "mic"}, "muted": True,
            }))
            for _ in range(6):
                m = await recv(ws2)
                if m["t"] == "state":
                    assert m["muted"] is True
                    break
            else:
                raise AssertionError("no state confirmation")
            await ws2.close()
    run(body())


def test_pair_wrong_code_fails(tmp_path):
    async def body():
        auth, pairing = _make_auth(tmp_path)
        async with engine_client(authenticator=auth) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await ws.send_str(json.dumps({"t": "hello", "token": ""}))
            await recv(ws)  # need_pair
            good = pairing.current_code()
            bad = "999999" if good != "999999" else "000000"
            await ws.send_str(json.dumps({
                "t": "pair", "code": bad, "device": {"id": "d", "name": "n"},
            }))
            assert (await recv(ws))["t"] == "pair_fail"
            await ws.close()
    run(body())
