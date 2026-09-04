"""Device authenticator: the real auth gate that replaces P0's NullAuth.

Implements the server's Authenticator protocol. Pre-auth, a connection may only
`hello` (present an existing token) or `pair` (redeem a one-time code). Everything
else is refused by the server until the client is authed.
"""
from __future__ import annotations

from typing import Any

from ..log import get_logger
from .allowlist import AllowList
from .pairing import PairingManager
from .tokens import hash_token, new_token

log = get_logger("security.auth")


class DeviceAuthenticator:
    def __init__(self, allowlist: AllowList, pairing: PairingManager, salt: str) -> None:
        self._allow = allowlist
        self._pairing = pairing
        self._salt = salt

    def requires_auth(self) -> bool:
        return True

    async def handle_preauth(self, client, msg: dict[str, Any]) -> bool:
        t = msg.get("t")
        if t == "hello":
            return await self._hello(client, msg)
        if t == "pair":
            return await self._pair(client, msg)
        return False

    async def _hello(self, client, msg: dict[str, Any]) -> bool:
        token = msg.get("token")
        device_id = self._allow.find_by_token(token) if token else None
        if device_id:
            client.authed = True
            client.device_id = device_id
            log.info("device authed via token: %s", device_id)
            return True
        # No/invalid token: tell the client to show the pairing screen.
        await client.send({"t": "need_pair"})
        return False

    async def _pair(self, client, msg: dict[str, Any]) -> bool:
        device = msg.get("device") or {}
        dev_id = str(device.get("id") or "").strip()
        dev_name = (str(device.get("name") or "device").strip() or "device")[:60]
        code = str(msg.get("code") or "")

        if not dev_id:
            await client.send({"t": "pair_fail", "reason": "missing device id"})
            return False

        if not self._pairing.verify(code):
            log.info("pairing failed for device %s", dev_id)
            await client.send({"t": "pair_fail", "reason": "invalid or expired code"})
            return False

        token = new_token()
        self._allow.add(dev_id, dev_name, hash_token(token, self._salt))
        client.authed = True
        client.device_id = dev_id
        log.info("device paired: %s (%s)", dev_name, dev_id)
        await client.send({"t": "pair_ok", "token": token})
        return True
