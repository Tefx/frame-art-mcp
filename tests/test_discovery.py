import asyncio
import json

import httpx
import pytest

from frame_art_mcp import discovery
from frame_art_mcp.discovery import Device, Discovery, device_directory
from frame_art_mcp.ledger import atomic_json


@pytest.fixture
def lan(monkeypatch, tmp_path):
    devices = {}
    calls = []

    async def probe(ip, timeout):
        calls.append((ip, timeout))
        return devices.get(ip)

    async def ssdp():
        return {"10.0.0.2", "10.0.0.3"}

    monkeypatch.setattr(discovery, "probe", probe)
    monkeypatch.setattr(discovery, "ssdp_hosts", ssdp)
    monkeypatch.setattr(discovery, "subnet_hosts", lambda: ["10.0.0.4", "10.0.0.5"])
    return Discovery(tmp_path), devices, calls


async def test_first_run_binding_and_cached_fast_path(lan):
    resolver, devices, calls = lan
    devices["10.0.0.2"] = Device("10.0.0.2", "uuid:a", "Frame", "model")
    first = await resolver.resolve()
    assert first.status == "ok" and first.device.duid == "uuid:a"
    assert ("10.0.0.4", 0.4) in calls
    cached = json.loads((resolver.root / "devices.json").read_text())
    assert cached["bound_duid"] == "uuid:a"
    calls.clear()
    assert (await resolver.resolve()).device == first.device
    assert calls == [("10.0.0.2", 0.6)]


async def test_ssdp_and_subnet_relocation_never_rebind(lan):
    resolver, devices, calls = lan
    atomic_json(
        resolver.root / "devices.json",
        {"bound_duid": "uuid:a", "devices": {"uuid:a": {"ip": "10.0.0.99"}}},
    )
    devices["10.0.0.2"] = Device("10.0.0.2", "uuid:b", "Other Frame", "model")
    devices["10.0.0.4"] = Device("10.0.0.4", "uuid:a", "Original Frame", "model")
    result = await resolver.resolve()
    assert result.device.ip == "10.0.0.4"
    del devices["10.0.0.4"]
    result = await resolver.resolve()
    assert result.status == "target_unavailable"
    assert (
        json.loads((resolver.root / "devices.json").read_text())["bound_duid"]
        == "uuid:a"
    )


async def test_multiple_devices_across_discovery_tiers_are_ambiguous(lan):
    resolver, devices, calls = lan
    devices["10.0.0.2"] = Device("10.0.0.2", "uuid:a", "One", "model")
    devices["10.0.0.4"] = Device("10.0.0.4", "uuid:b", "Two", "model")
    result = await resolver.resolve()
    assert result.status == "ambiguous_multiple_tvs"
    assert {d.duid for d in result.candidates} == {"uuid:a", "uuid:b"}
    assert not (resolver.root / "devices.json").exists()


async def test_global_duid_is_binding_constraint_even_for_host(lan, monkeypatch):
    resolver, devices, calls = lan
    monkeypatch.setenv("FRAME_TV_DUID", "uuid:a")
    devices["10.0.0.2"] = Device("10.0.0.2", "uuid:b", "Other", "model")
    assert (await resolver.resolve("10.0.0.2")).status == "duid_mismatch"
    assert (await resolver.resolve()).status == "target_unavailable"
    atomic_json(resolver.root / "devices.json", {"bound_duid": "uuid:b", "devices": {}})
    calls.clear()
    assert (await resolver.resolve()).status == "duid_mismatch"
    assert calls == []


async def test_host_override_preserves_binding_and_uses_explicit_timeout(
    lan, monkeypatch
):
    resolver, devices, calls = lan
    device = Device("10.0.0.2", "uuid:a", "Frame", "model")
    devices[device.ip] = device
    assert (await resolver.resolve(device.ip)).device == device
    assert calls == [(device.ip, 1.5)]
    assert not (resolver.root / "devices.json").exists()
    await resolver.resolve()
    before = (resolver.root / "devices.json").read_bytes()
    devices["10.0.0.5"] = Device("10.0.0.5", "uuid:b", "Other", "model")
    assert (await resolver.resolve("10.0.0.5")).status == "duid_mismatch"
    monkeypatch.setenv("FRAME_TV_HOST", "10.0.0.2")
    assert (await resolver.resolve()).device == device
    assert (resolver.root / "devices.json").read_bytes() == before


async def test_subnet_probe_concurrency_bound(lan, monkeypatch):
    resolver, _, _ = lan
    active, peak = 0, 0

    async def probe(ip, timeout):
        nonlocal active, peak
        if timeout == 0.4:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
        return None

    monkeypatch.setattr(discovery, "probe", probe)
    monkeypatch.setattr(
        discovery, "subnet_hosts", lambda: [f"10.0.0.{i}" for i in range(1, 255)]
    )
    assert (await resolver.resolve()).status == "target_unavailable"
    assert peak == 48 and active == 0


async def test_http_probe_validates_frame_and_duid(monkeypatch):
    original = httpx.AsyncClient
    payload = {
        "device": {
            "FrameTVSupport": "true",
            "duid": "uuid:a",
            "name": "Frame",
            "modelName": "M",
            "resolution": "3840x2160",
        }
    }

    def handle(request):
        assert str(request.url) == "http://10.0.0.2:8001/api/v2/"
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    result = await discovery.probe("10.0.0.2", 0.4)
    assert result == Device("10.0.0.2", "uuid:a", "Frame", "M")
    payload["device"]["FrameTVSupport"] = "false"
    assert await discovery.probe("10.0.0.2", 0.4) is None
    payload["device"] = {"FrameTVSupport": "true"}
    assert await discovery.probe("10.0.0.2", 0.4) is None


def test_sanitized_duid_directory(tmp_path):
    assert device_directory(tmp_path, "uuid:a/b:c?d") == tmp_path / "uuid_a_b_c_d"


@pytest.mark.parametrize(
    "cache",
    [
        {"bound_duid": [], "devices": {}},
        {"bound_duid": "uuid:a", "devices": {"uuid:a": None}},
        {"bound_duid": "uuid:a", "devices": {"uuid:a": {"ip": 42}}},
    ],
)
async def test_malformed_binding_cache_fails_closed(lan, cache):
    resolver, _, calls = lan
    atomic_json(resolver.root / "devices.json", cache)
    assert (await resolver.resolve()).status == "device_cache_invalid"
    assert calls == []


async def test_ssdp_wire_request_and_location_response_parsing(monkeypatch):
    class Socket:
        def __init__(self, *args):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def setblocking(self, value):
            assert value is False

    monkeypatch.setattr(discovery.socket, "socket", Socket)
    loop = asyncio.get_running_loop()
    messages = []

    async def send(sock, message, address):
        messages.append((message, address))

    responses = [
        b"HTTP/1.1 200 OK\r\nLOCATION: http://10.0.0.9:8001/description.xml\r\n\r\n"
    ]

    async def receive(sock, size):
        if responses:
            return responses.pop(), ("10.0.0.8", 1900)
        raise TimeoutError

    monkeypatch.setattr(loop, "sock_sendto", send)
    monkeypatch.setattr(loop, "sock_recvfrom", receive)
    assert await discovery.ssdp_hosts() == {"10.0.0.9"}
    message, address = messages[0]
    assert address == ("239.255.255.250", 1900)
    assert message.startswith(b"M-SEARCH * HTTP/1.1\r\n")
    assert b"ST: urn:samsung.com:device:RemoteControlReceiver:1\r\n" in message
    assert message.endswith(b"\r\n\r\n")


def test_active_private_subnet_routing_lookup_without_sending(monkeypatch):
    class Socket:
        def __init__(self, *args):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def connect(self, address):
            assert address == ("192.0.2.1", 9)

        def getsockname(self):
            return ("10.8.3.12", 1234)

    monkeypatch.setattr(discovery.socket, "socket", Socket)
    hosts = discovery.subnet_hosts()
    assert len(hosts) == 254
    assert hosts[0] == "10.8.3.1" and hosts[-1] == "10.8.3.254"


async def test_corrupt_device_cache_fails_closed(lan):
    resolver, devices, _ = lan
    devices["10.0.0.2"] = Device("10.0.0.2", "uuid:b", "Other", "model")
    (resolver.root / "devices.json").write_text("{")
    assert (await resolver.resolve()).status == "device_cache_invalid"
    assert (resolver.root / "devices.json").read_text() == "{"
