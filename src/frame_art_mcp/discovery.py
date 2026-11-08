"""Bound-DUID discovery; relocation never changes the target identity."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlsplit

import httpx

from .ledger import atomic_json


def state_root() -> Path:
    return Path(
        os.environ.get("FRAME_ART_STATE_DIR", "~/.local/share/frame-art-mcp")
    ).expanduser()


def device_directory(root: Path, duid: str) -> Path:
    return root / re.sub(r"[^A-Za-z0-9_.-]", "_", duid)


@dataclass(frozen=True)
class Device:
    ip: str
    duid: str
    name: str
    model: str
    resolution: tuple[int, int] = (3840, 2160)


@dataclass(frozen=True)
class DiscoveryResult:
    status: str
    device: Device | None = None
    candidates: tuple[Device, ...] = ()
    cached_device: Device | None = None


async def probe(host: str, timeout: float) -> Device | None:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), trust_env=False
        ) as client:
            response = await client.get(f"http://{host}:8001/api/v2/")
            response.raise_for_status()
            payload = response.json()
        dev = payload["device"]
        if not isinstance(dev, dict):
            return None
        if (
            dev.get("FrameTVSupport") != "true"
            or not isinstance(dev.get("duid"), str)
            or not dev["duid"]
        ):
            return None
        resolution = tuple(
            int(x) for x in dev.get("resolution", "3840x2160").split("x")
        )
        if len(resolution) != 2 or min(resolution) <= 0:
            return None
        return Device(
            host,
            dev["duid"],
            dev.get("name", payload.get("name", "Samsung Frame")),
            dev.get("modelName", ""),
            resolution,
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


async def ssdp_hosts() -> set[str]:
    message = (
        "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\nMX: 1\r\n'
        "ST: urn:samsung.com:device:RemoteControlReceiver:1\r\n\r\n"
    )
    loop = asyncio.get_running_loop()
    found: set[str] = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        try:
            await loop.sock_sendto(
                sock, message.encode("ascii"), ("239.255.255.250", 1900)
            )
            async with asyncio.timeout(1.0):
                while True:
                    data, address = await loop.sock_recvfrom(sock, 65535)
                    headers = data.decode("latin1").splitlines()
                    location = next(
                        (
                            line.split(":", 1)[1].strip()
                            for line in headers
                            if line.lower().startswith("location:")
                        ),
                        "",
                    )
                    found.add(urlsplit(location).hostname or address[0])
        except (TimeoutError, OSError):
            return found


def subnet_hosts() -> list[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # Routing lookup only; UDP connect sends no packet.
            sock.connect(("192.0.2.1", 9))
            local = ipaddress.IPv4Address(sock.getsockname()[0])
        private_ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        if not any(local in ipaddress.IPv4Network(net) for net in private_ranges):
            return []
        return [
            str(ip) for ip in ipaddress.IPv4Network(f"{local}/24", strict=False).hosts()
        ]
    except OSError:
        return []


class Discovery:
    def __init__(self, root: Path | None = None):
        self.root = root if root is not None else state_root()
        self._lock = asyncio.Lock()

    async def resolve(self, host: str | None = None) -> DiscoveryResult:
        async with self._lock:
            path = self.root / "devices.json"
            try:
                cache = json.loads(path.read_text())
                if not isinstance(cache, dict) or not isinstance(
                    cache.get("devices"), dict
                ):
                    return DiscoveryResult("device_cache_invalid")
                if cache.get("bound_duid") is not None and (
                    not isinstance(cache["bound_duid"], str) or not cache["bound_duid"]
                ):
                    return DiscoveryResult("device_cache_invalid")
                if any(
                    not isinstance(item, dict) or not isinstance(item.get("ip"), str)
                    for item in cache["devices"].values()
                ):
                    return DiscoveryResult("device_cache_invalid")
            except FileNotFoundError:
                cache = {"bound_duid": None, "devices": {}}
            except (ValueError, OSError):
                # A lost binding must not silently authorize a replacement device.
                return DiscoveryResult("device_cache_invalid")
            bound = cache.get("bound_duid")
            env_duid = os.environ.get("FRAME_TV_DUID") or None
            if bound and env_duid and bound != env_duid:
                return DiscoveryResult("duid_mismatch")
            desired = env_duid or bound
            override = host or os.environ.get("FRAME_TV_HOST")
            if override:
                device = await probe(override, 1.5)
                if device is None:
                    return DiscoveryResult("target_unavailable")
                if desired and device.duid != desired:
                    return DiscoveryResult("duid_mismatch")
                # Explicit overrides do not establish or replace the automatic binding.
                return DiscoveryResult("ok", device)
            cached_device = None
            if desired:
                entry = cache["devices"].get(desired, {})
                if entry.get("ip"):
                    cached_device = Device(
                        entry["ip"],
                        desired,
                        entry.get("name", "Samsung Frame"),
                        entry.get("model", ""),
                        tuple(entry.get("resolution", (3840, 2160))),
                    )
                cached = entry.get("ip")
                if cached:
                    device = await probe(cached, 0.6)
                    if device and device.duid == desired:
                        return self._remember(cache, device)
            candidates: dict[str, Device] = {}
            for device in await asyncio.gather(
                *(probe(ip, 0.6) for ip in await ssdp_hosts())
            ):
                if device and (not desired or device.duid == desired):
                    candidates[device.duid] = device
            if desired and desired in candidates:
                return self._remember(cache, candidates[desired])
            sem = asyncio.Semaphore(48)

            async def scan(ip: str) -> None:
                async with sem:
                    device = await probe(ip, 0.4)
                    if device and (not desired or device.duid == desired):
                        candidates[device.duid] = device

            try:
                async with asyncio.timeout(2.5):
                    await asyncio.gather(*(scan(ip) for ip in subnet_hosts()))
            except TimeoutError:
                # Keep completed observations, never bind an incompletely scanned first run.
                if not desired:
                    return DiscoveryResult(
                        "discovery_incomplete", candidates=tuple(candidates.values())
                    )
            if not candidates:
                return DiscoveryResult(
                    "target_unavailable", cached_device=cached_device
                )
            if len(candidates) > 1:
                return DiscoveryResult(
                    "ambiguous_multiple_tvs", candidates=tuple(candidates.values())
                )
            return self._remember(cache, next(iter(candidates.values())))

    def _remember(self, cache: dict, device: Device) -> DiscoveryResult:
        cache["bound_duid"] = device.duid
        cache["devices"][device.duid] = {**asdict(device), "last_seen": time.time()}
        atomic_json(self.root / "devices.json", cache)
        return DiscoveryResult("ok", device)
