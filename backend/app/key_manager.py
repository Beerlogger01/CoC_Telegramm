"""Single-process serialized developer-portal recovery; manual token is fallback."""

import asyncio
import base64
import ipaddress
import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)
PORTAL = "https://developer.clashofclans.com/api/"


class KeyManager:
    def __init__(self, settings, client, path):
        self.settings, self.client, self.path = settings, client, Path(path)
        self.lock = asyncio.Lock()
        self.next_attempt = 0
        self.status = "MANUAL"
        self.storage_valid = True
        self.state = {"owner": secrets.token_hex(12), "ids": []}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                if (not isinstance(saved, dict) or not isinstance(saved.get("owner"), str)
                        or not saved["owner"] or not isinstance(saved.get("ids"), list)
                        or not all(isinstance(value, str) for value in saved["ids"])):
                    raise ValueError("Invalid ownership metadata")
                self.state = saved
            except (OSError, ValueError):
                self.storage_valid = False
                self.status = "KEY_STATE_INVALID"
                log.warning("CoC key ownership metadata invalid; manual credentials retained")
        else:
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    async def recover(self):
        if not self.settings.coc_auto_keys or not self.storage_valid:
            return False
        async with self.lock:
            if time.monotonic() < self.next_attempt:
                return False
            self.next_attempt = time.monotonic() + 300
            try:
                async with httpx.AsyncClient(timeout=15) as portal:

                    async def call(route, data=None):
                        response = await portal.post(PORTAL + route, json=data or {})
                        response.raise_for_status()
                        return response.json()

                    login = await call(
                        "login",
                        {
                            "email": self.settings.coc_email,
                            "password": self.settings.coc_password,
                        },
                    )
                    # Portal-signed temporary token reports the source address seen by Supercell.
                    payload = login["temporaryAPIToken"].split(".")[1]
                    claims = json.loads(
                        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
                    )
                    addresses = {
                        str(ipaddress.IPv4Address(cidr.split("/")[0]))
                        for limit in claims["limits"]
                        for cidr in limit.get("cidrs", [])
                        if ":" not in cidr
                    }
                    if len(addresses) != 1:
                        raise ValueError("Ambiguous IPv4")
                    ip = addresses.pop()
                    if not ipaddress.ip_address(ip).is_global:
                        raise ValueError("Not a public IPv4")
                    keys = (await call("apikey/list"))["keys"]
                    name = "ha-coc-" + self.state["owner"]
                    owned = [
                        k
                        for k in keys
                        if k.get("name") == name and k.get("id") in self.state["ids"]
                    ]
                    matching = [
                        k
                        for k in owned
                        if any(
                            ipaddress.ip_address(ip)
                            in ipaddress.ip_network(c, strict=False)
                            for c in k.get("cidrRanges", [])
                        )
                    ]
                    if self.state.get("ip") == ip and not matching:
                        self.status = "ACCESS_DENIED_UNCHANGED_IP"
                        return False
                    if matching:
                        candidate = matching[0]
                    else:
                        if len(keys) >= 10:
                            self.status = "KEY_LIMIT_MANUAL_ACTION"
                            return False
                        candidate = (
                            await call(
                                "apikey/create",
                                {
                                    "name": name,
                                    "description": "Home Assistant managed key",
                                    "cidrRanges": [ip],
                                    "scopes": ["clash"],
                                },
                            )
                        )["key"]
                        self.state["ids"].append(candidate["id"])
                        self.save()
                    probe = await self.client.get(
                        self.settings.coc_api_base
                        + "/clans/"
                        + self.settings.coc_clan_tag.replace("#", "%23"),
                        headers={"Authorization": "Bearer " + candidate["key"]},
                    )
                    if probe.status_code != 200:
                        self.status = "KEY_VALIDATION_FAILED"
                        return False
                    self.client.headers["Authorization"] = "Bearer " + candidate["key"]
                    self.state["ip"] = ip
                    self.save()
                    self.status = "ACTIVE"
                    # Revoke only recorded, owned obsolete keys AFTER verified switch.
                    for old in owned:
                        if old["id"] != candidate["id"]:
                            await call("apikey/revoke", {"id": old["id"]})
                            self.state["ids"].remove(old["id"])
                            self.save()
                    return True
            except Exception:
                self.status = "PORTAL_RECOVERY_FAILED"
                log.warning("CoC key recovery unavailable; keeping current credentials")
                return False
