import asyncio
import io
import json
from pathlib import Path
import re
import threading

import httpx
import numpy as np
from PIL import Image
import pytest
from samsungtvws import exceptions

from frame_art_mcp import discovery, server, tv_client
from frame_art_mcp.discovery import Device, DiscoveryResult
from frame_art_mcp.guide import OPTICS_GUIDE
from frame_art_mcp.ledger import DeviceIOError, Ledger, atomic_json, load_ledger
from frame_art_mcp.optics import Conditioned
from frame_art_mcp.server import FrameService, SequenceStep
from frame_art_mcp.tv_client import DeviceWorker, SamsungClient


@pytest.fixture
def app(tmp_path, tv, monkeypatch):
    class BoundDiscovery:
        root = tmp_path

        async def resolve(self, host=None):
            return DiscoveryResult(
                "ok", Device("10.0.0.2", "uuid:test", "Frame", "model")
            )

    instance = FrameService(BoundDiscovery(), lambda device, directory: tv)
    monkeypatch.setattr(server, "_service", instance)
    return instance


@pytest.fixture
def metadata_http(monkeypatch):
    payload = {
        "device": {
            "duid": "uuid:test",
            "FrameTVSupport": "true",
            "PowerState": "on",
            "TokenAuthSupport": "true",
        }
    }
    original = httpx.Client

    def handle(request):
        assert str(request.url) == "http://10.0.0.2:8001/api/v2/"
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    return payload["device"]


def client(tmp_path):
    return SamsungClient(Device("10.0.0.2", "uuid:test", "Frame", "model"), tmp_path)


@pytest.mark.parametrize(
    "power,expected", [("standby", "off"), ("unrecognized", "unknown")]
)
def test_authoritative_http_overrules_stale_artmode(
    tmp_path, metadata_http, monkeypatch, power, expected
):
    metadata_http["PowerState"] = power

    def forbidden(*args, **kwargs):
        raise AssertionError("Standby/unknown must never open the Art WebSocket")

    monkeypatch.setattr(tv_client, "SamsungTVArt", forbidden)
    assert client(tmp_path).probe(read_only=True)[0] == expected


@pytest.mark.parametrize("token", [None, "", "  "])
def test_readonly_missing_token_never_opens_websocket(
    tmp_path, metadata_http, monkeypatch, token
):
    if token is not None:
        (tmp_path / "token.txt").write_text(token)

    def forbidden(*args, **kwargs):
        raise AssertionError("Read-only inspection must not pair")

    monkeypatch.setattr(tv_client, "SamsungTVArt", forbidden)
    assert client(tmp_path).probe(read_only=True) == ("unknown", "pairing_required")


def test_rejected_token_has_no_unauthenticated_retry(
    tmp_path, metadata_http, monkeypatch
):
    (tmp_path / "token.txt").write_text("rejected")
    attempts = []

    class Art:
        connection = None

        def __init__(self, *args, **kwargs):
            attempts.append(kwargs["token"])

        def get_artmode(self):
            raise exceptions.UnauthorizedError("rejected")

    monkeypatch.setattr(tv_client, "SamsungTVArt", Art)
    assert client(tmp_path).probe(read_only=True) == ("unknown", "pairing_required")
    assert attempts == ["rejected"]


@pytest.mark.parametrize(
    "artmode,expected", [("on", "art"), ("off", "tv"), ("unexpected", "unknown")]
)
def test_mode_and_api_adapter_contract(
    tmp_path, metadata_http, monkeypatch, artmode, expected
):
    (tmp_path / "token.txt").write_text("valid")
    calls = []

    class Art:
        connection = None

        def __init__(self, *args, **kwargs):
            assert kwargs["port"] == 8002 and kwargs["timeout"] == 3.0

        def get_artmode(self):
            return artmode

        def get_current(self):
            return {"content_id": "base"}

        def available(self, category):
            assert category == "MY-C0002"
            return [{"content_id": "owned"}]

        def upload(self, data, **kwargs):
            calls.append((data, kwargs, self.timeout))
            return "new"

        def select_image(self, content_id, show):
            assert content_id == "new" and show is True

        def delete(self, content_id):
            return content_id == "owned"

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(tv_client, "SamsungTVArt", Art)
    tv = client(tmp_path)
    assert tv.probe(read_only=True)[0] == expected
    if expected == "art":
        assert tv.current() == "base" and tv.available() == {"owned"}
        path = tmp_path / "image.jpg"
        path.write_bytes(b"jpeg")
        assert tv.upload(path) == "new"
        assert calls == [
            (
                b"jpeg",
                {"matte": "none", "portrait_matte": "none", "file_type": "jpg"},
                15.0,
            )
        ]
        assert tv.art.timeout == 4.0
        tv.select("new")
        assert tv.delete("owned")
    tv.close()
    assert calls[-1] == "closed"


def test_malformed_art_api_json_and_broken_timeout_fail_closed(
    tmp_path, metadata_http, monkeypatch
):
    (tmp_path / "token.txt").write_text("valid")

    class Art:
        connection = None

        def __init__(self, *args, **kwargs):
            return None

        def get_artmode(self):
            return "on"

        def get_current(self):
            raise ValueError("Malformed Art API JSON")

        def close(self):
            return None

    monkeypatch.setattr(tv_client, "SamsungTVArt", Art)
    tv = client(tmp_path)
    assert tv.probe(read_only=True)[0] == "art"
    with pytest.raises(DeviceIOError, match="ValueError"):
        tv.current()

    class BrokenSocket:
        def settimeout(self, seconds):
            raise OSError("Disconnected")

    tv.art.connection = BrokenSocket()
    assert tv.probe(read_only=True) == ("unknown", "ws_error")


def test_dhcp_replacement_cannot_pass_mode_gate(tmp_path, metadata_http):
    metadata_http["duid"] = "uuid:other"
    assert client(tmp_path).probe() == ("unknown", "duid_mismatch")


async def tool(name, **arguments):
    content, structured = await server.mcp.call_tool(name, arguments)
    assert json.loads(content[0].text) == structured
    return structured


@pytest.mark.parametrize("grade", ["raw", "matte_natural"])
async def test_registered_mcp_tools_resource_and_real_local_optical_to_device_path(
    app, tmp_path, tv, grade
):
    try:
        tools = {t.name: t for t in await server.mcp.list_tools()}
        assert set(tools) == {
            "frame_inspect",
            "frame_put",
            "frame_switch",
            "frame_clear",
        }
        assert tools["frame_inspect"].annotations.readOnlyHint is True
        assert tools["frame_clear"].annotations.destructiveHint is True
        assert tools["frame_put"].inputSchema["properties"]["fit"]["enum"] == [
            "contain",
            "cover",
            "pad_paper",
            "pad_slate",
        ]
        inspection = await tool("frame_inspect", include_optics_guide=True)
        assert inspection["tv_mode"] == "art" and inspection["safe_to_project"] is True
        resources = await server.mcp.read_resource("frame://guide/matte-optics")
        assert resources[0].content == inspection["optics_guide"] == OPTICS_GUIDE
        assert not any(c[0] in ("upload", "select", "delete") for c in tv.calls)
        source = tmp_path / "diagram.png"
        Image.new("RGB", (4000, 1), (40, 60, 80)).save(source)
        dry = await tool(
            "frame_put", image_path=str(source), optical_grade=grade, dry_run=True
        )
        dry_bytes = Path(dry["preview_full_path"]).read_bytes()
        assert dry["status"] == "dry_run" and not any(
            c[0] == "upload" for c in tv.calls
        )
        staged = await tool(
            "frame_put",
            image_path=str(source),
            optical_grade=grade,
            show=False,
            request_id="put",
        )
        assert staged["status"] == "staged" and staged["projected"] is False
        assert Path(staged["cached_image_path"]).read_bytes() == dry_bytes
        assert (
            await tool(
                "frame_put",
                image_path=str(source),
                optical_grade=grade,
                show=False,
                request_id="put",
            )
        )["content_id"] == staged["content_id"]
        switched = await tool("frame_switch", target="main", request_id="switch")
        assert (
            switched["status"] == "switched" and tv.current_id == staged["content_id"]
        )
        cleared = await tool("frame_clear", request_id="clear")
        assert cleared["status"] == "cleared"
        assert tv.current_id == "USER_BASE" and staged["content_id"] not in tv.images
        assert (await tool("frame_clear", request_id="clear"))[
            "deleted_content_ids"
        ] == [staged["content_id"]]
        selections = [c for c in tv.calls if c[0] == "select"]
        assert (await tool("frame_switch", target="main", request_id="switch"))[
            "status"
        ] == "switched"
        assert [c for c in tv.calls if c[0] == "select"] == selections
        assert (await tool("frame_clear", slots=["main"], request_id="clear"))[
            "status"
        ] == "request_id_conflict"
        assert not list((tmp_path / "uuid_test" / "blobs").glob("tmp_*.jpg"))
    finally:
        await app.close()


async def test_input_validation_before_network_or_image_work(app):
    try:
        with pytest.raises(ValueError, match="invalid_slot"):
            await app.put("missing.png", slot="../escape")
        with pytest.raises(ValueError, match="invalid_ttl"):
            await app.put("missing.png", ttl_sec=9)
        with pytest.raises(ValueError, match="invalid_loop"):
            await app.switch("main", loop=101)
        with pytest.raises(ValueError, match="invalid_sequence_length"):
            await app.switch([])
        with pytest.raises(ValueError):
            await app.switch([SequenceStep(slot="main", dwell_ms=249)])
        assert app.workers == {}
    finally:
        await app.close()


def seed_slots(directory, tv, *, expires_at=None, active_sequence=None):
    (directory / "blobs").mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(("a", "b"), 1):
        tv.images.add(f"MY_SEED{index}")
        (directory / "blobs" / f"{name}__r{index}.jpg").write_bytes(b"seed")
    ledger = Ledger.model_validate(
        {
            "duid": "uuid:test",
            "control_generation": 5,
            "next_revision": 3,
            "baseline_content_id": "USER_BASE",
            "owned_ids": ["MY_SEED1", "MY_SEED2"],
            "slots": {
                name: {
                    "revision": index,
                    "content_id": f"MY_SEED{index}",
                    "cached_image_path": str(
                        directory / "blobs" / f"{name}__r{index}.jpg"
                    ),
                    "created_at": "2026-01-01T00:00:00Z",
                    "expires_at": expires_at,
                }
                for index, name in enumerate(("a", "b"), 1)
            },
            "active_sequence": active_sequence,
        }
    )
    atomic_json(directory / "ledger.json", ledger.model_dump())


async def test_process_restart_never_resumes_persisted_sequence(app, tmp_path, tv):
    seed_slots(
        tmp_path / "uuid_test",
        tv,
        active_sequence={
            "generation": 5,
            "steps": [{"slot": "a", "dwell_ms": 250}],
            "loop": 1,
            "restore_on_done": False,
        },
    )
    try:
        result = await app.inspect()
        assert result["active_sequence"] is None
        assert app.sequences == {}
        assert not any(c[0] == "select" for c in tv.calls)
    finally:
        await app.close()


async def test_active_ttl_timer_fires_and_restores_without_client_call(
    app, tmp_path, tv
):
    now = 1000.0
    sleeping, wake = asyncio.Event(), asyncio.Event()

    async def sleep(delay):
        assert 0.0 <= delay <= 10.0
        sleeping.set()
        await wake.wait()

    app.clock, app.sleep = lambda: now, sleep
    seed_slots(tmp_path / "uuid_test", tv, expires_at=1010.0)
    deletion = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_delete = tv.delete

    def delete(cid):
        result = original_delete(cid)
        if cid == "MY_SEED2":
            loop.call_soon_threadsafe(deletion.set)
        return result

    tv.delete = delete
    try:
        await app.inspect()
        await asyncio.wait_for(sleeping.wait(), timeout=2)
        now = 1010.0
        wake.set()
        await asyncio.wait_for(deletion.wait(), timeout=2)
        await asyncio.gather(*list(app.transactions))
        assert not {"MY_SEED1", "MY_SEED2"} & tv.images
        assert app.states["uuid:test"].slots == {}
    finally:
        await app.close()


async def test_ttl_timer_rechecks_deadline_after_wall_clock_moves_back(
    app, tmp_path, tv
):
    now = 1000.0
    waiting = asyncio.Queue()

    async def sleep(delay):
        release = asyncio.Event()
        await waiting.put((delay, release))
        await release.wait()

    app.clock, app.sleep = lambda: now, sleep
    directory = tmp_path / "uuid_test"
    seed_slots(directory, tv, expires_at=1010.0)
    state = load_ledger(directory, "uuid:test")
    state.slots["b"].expires_at = None
    atomic_json(directory / "ledger.json", state.model_dump())
    deleted = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = tv.delete

    def delete(cid):
        result = original(cid)
        loop.call_soon_threadsafe(deleted.set)
        return result

    tv.delete = delete
    try:
        await app.inspect()
        delay, release = await asyncio.wait_for(waiting.get(), timeout=2)
        assert delay == 10.0
        now = 990.0
        release.set()
        delay, release = await asyncio.wait_for(waiting.get(), timeout=2)
        assert delay == 20.0
        assert not deleted.is_set()
        now = 1010.0
        release.set()
        await asyncio.wait_for(deleted.wait(), timeout=2)
        await asyncio.gather(*list(app.transactions))
        assert "MY_SEED1" not in tv.images and "MY_SEED2" in tv.images
        assert set(app.states["uuid:test"].slots) == {"b"}
    finally:
        await app.close()


async def test_background_sequence_restores_on_completion(app, tmp_path, tv):
    dwells = []

    async def sleep(delay):
        dwells.append(delay)
        await asyncio.sleep(0)

    app.sleep = sleep
    seed_slots(tmp_path / "uuid_test", tv)
    restored = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = tv.select

    def select(cid):
        original(cid)
        if cid == "USER_BASE":
            loop.call_soon_threadsafe(restored.set)

    tv.select = select
    try:
        result = await app.switch(
            [
                SequenceStep(slot="a", dwell_ms=250),
                SequenceStep(slot="b", dwell_ms=250),
            ],
            restore_on_done=True,
        )
        assert result["status"] == "sequence_started"
        await asyncio.wait_for(restored.wait(), timeout=2)
        await asyncio.gather(*list(app.transactions))
        assert [c[1] for c in tv.calls if c[0] == "select"] == [
            "MY_SEED1",
            "MY_SEED2",
            "USER_BASE",
        ]
        assert app.states["uuid:test"].active_sequence is None
        assert dwells == [0.25, 0.25]
    finally:
        await app.close()


async def test_unreachable_bound_target_still_allows_local_clear_without_rebinding(
    tmp_path, monkeypatch, tv
):
    device = Device("10.0.0.2", "uuid:test", "Frame", "model")
    atomic_json(
        tmp_path / "devices.json",
        {
            "bound_duid": device.duid,
            "devices": {
                device.duid: {
                    "ip": device.ip,
                    "name": device.name,
                    "model": device.model,
                }
            },
        },
    )
    seed_slots(tmp_path / "uuid_test", tv)

    async def unavailable(ip, timeout):
        return None

    async def no_ssdp():
        return set()

    monkeypatch.setattr(discovery, "probe", unavailable)
    monkeypatch.setattr(discovery, "ssdp_hosts", no_ssdp)
    monkeypatch.setattr(discovery, "subnet_hosts", lambda: [])
    tv.mode = "unknown"
    app = FrameService(discovery.Discovery(tmp_path), lambda device, directory: tv)
    try:
        assert (await app.put("does-not-exist.png"))["status"] == "target_unavailable"
        result = await app.clear()
        assert result["status"] == "deferred_offline_clear"
        assert set(result["cleared_slots"]) == {"a", "b"}
        assert set(result["pending_delete_ids"]) == {"MY_SEED1", "MY_SEED2"}
        assert not any(c[0] in ("upload", "select", "delete") for c in tv.calls)
        assert (await app.switch("a"))["status"] == "target_unavailable"
        inspected = await app.inspect(include_optics_guide=True)
        assert inspected["tv_reachable"] is False
        assert inspected["optics_guide"] == OPTICS_GUIDE
        assert inspected["slots"] == {}
        assert (
            json.loads((tmp_path / "devices.json").read_text())["bound_duid"]
            == "uuid:test"
        )
        tv.mode = "art"

        async def reachable(ip, timeout):
            return device

        monkeypatch.setattr(discovery, "probe", reachable)
        converged = await app.clear()
        assert (
            converged["status"] == "cleared" and converged["pending_delete_ids"] == []
        )
        assert {"MY_SEED1", "MY_SEED2"}.isdisjoint(tv.images)
        assert {"USER_BASE", "USER_PHOTO"} <= tv.images
    finally:
        await app.close()


async def test_mcp_inspect_pairing_guard_and_guide_through_real_adapter(
    app, metadata_http
):
    app.client_factory = SamsungClient
    try:
        result = await tool("frame_inspect", include_optics_guide=True)
        assert result["tv_mode"] == "unknown" and result["safe_to_project"] is False
        assert result["reason"] == "pairing_required"
        assert result["optics_guide"] == OPTICS_GUIDE
    finally:
        await app.close()


async def test_shutdown_during_discovery_cannot_create_late_worker(app, tv):
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(host=None):
        entered.set()
        await release.wait()
        return DiscoveryResult("ok", Device("10.0.0.2", "uuid:test", "Frame", "model"))

    app.discovery.resolve = resolve
    pending = asyncio.create_task(app.inspect())
    try:
        await entered.wait()
        await app.close()
        release.set()
        with pytest.raises(RuntimeError, match="server_closing"):
            await pending
        assert app.workers == {}
        assert not any(c[0] in ("upload", "select", "delete") for c in tv.calls)
    finally:
        release.set()
        await app.close()


async def test_cancelled_shutdown_still_drains_unfinished_renderer(
    app, monkeypatch, tv
):
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def render(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return Conditioned(b"jpeg", {"optical_diagnostics": {}})

    monkeypatch.setattr(server, "condition", render)
    pending = asyncio.create_task(app.put("unused.png"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        shutdown = asyncio.create_task(app.close())
        await asyncio.sleep(0)
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        with pytest.raises(RuntimeError, match="server_closing"):
            await pending
        assert all(worker.closed for worker in app.workers.values())
        assert not any(c[0] in ("upload", "select", "delete") for c in tv.calls)
    finally:
        release.set()
        await app.close()


async def test_one_shutdown_failure_still_drains_every_device_worker(app, tmp_path, tv):
    first, _ = await app._resolve(None)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def probe(*, read_only=False):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return "art", None

    tv.probe = probe
    second = DeviceWorker(tmp_path / "second", "uuid:second", lambda: tv)
    app.workers["uuid:second"] = second

    async def fail(action, params):
        assert action == "shutdown"
        raise PermissionError("Cannot persist first device shutdown")

    first.run = fail
    active = asyncio.create_task(second.run("inspect", {}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        shutdown = asyncio.create_task(app.close())
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        await active
        with pytest.raises(ExceptionGroup) as failures:
            await shutdown
        assert any(
            isinstance(error, PermissionError) for error in failures.value.exceptions
        )
        assert all(worker.closed for worker in app.workers.values())
    finally:
        release.set()
        await active
        for worker in app.workers.values():
            await worker.close()


async def test_cancelled_put_worker_finishes_and_cleans_temporary_blob(
    app, monkeypatch, tv
):
    monkeypatch.setattr(
        server,
        "condition",
        lambda *args, **kwargs: Conditioned(b"jpeg", {"optical_diagnostics": {}}),
    )
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = tv.upload

    def upload(path):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return original(path)

    tv.upload = upload
    pending = asyncio.create_task(
        app.put("unused.png", request_id="cancelled", ttl_sec=10)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        shutdown = asyncio.create_task(app.close())
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        await shutdown
        worker = app.workers["uuid:test"]
        ledger = load_ledger(worker.directory, "uuid:test")
        assert ledger.recent_requests["cancelled"]["status"] == "projected"
        assert ledger.displayed.content_id == tv.current_id
        assert ledger.displayed.expires_at is not None
        assert len(ledger.owned_ids) == 1
        assert not list((worker.directory / "blobs").glob("tmp_*.jpg"))
    finally:
        release.set()
        await app.close()


@pytest.mark.parametrize("change", ["human", "mode"])
async def test_background_sequence_cancels_on_observed_viewer_change(
    app, tmp_path, tv, change
):
    seed_slots(tmp_path / "uuid_test", tv)
    try:
        started = await app.switch(
            [SequenceStep(slot="a", dwell_ms=250), SequenceStep(slot="b", dwell_ms=250)]
        )
        sequence = app.sequences["uuid:test"][1]
        if change == "human":
            tv.current_id = "USER_PHOTO"
        else:
            tv.mode = "tv"
        observed = await app.inspect()
        await asyncio.gather(sequence, return_exceptions=True)
        assert observed["control_generation"] == started["control_generation"] + 1
        assert observed["active_sequence"] is None
        assert app.sequences == {}
        assert [c for c in tv.calls if c[0] == "select"] == [("select", "MY_SEED1")]
        assert set(observed["slots"]) == {"a", "b"}
    finally:
        await app.close()


def test_recipe_programs_execute_with_local_protocol_double(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Image.new("RGB", (32, 32), (120, 80, 40)).save("source.jpg")
    codes = re.findall(r"```python\n(.*?)```", OPTICS_GUIDE, re.S)
    assert len(codes) == 4

    class Response:
        def __init__(self, info=False):
            self.info = info

        def raise_for_status(self):
            return None

        def json(self):
            return {"width": 5000, "height": 3000}

        @property
        def content(self):
            stream = io.BytesIO()
            Image.new("RGB", (3840, 2160), (100, 90, 80)).save(stream, format="JPEG")
            return stream.getvalue()

    requests = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 30, "follow_redirects": True}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            requests.append(url)
            return Response(url.endswith("info.json"))

    monkeypatch.setattr(httpx, "Client", Client)
    for code in codes:
        assert len(code.splitlines()) == 15
        exec(compile(code, "optics_recipe", "exec"), {})
    assert requests[-1].endswith("/580,420,3840,2160/full/0/default.jpg")
    for name, size in (
        ("relief.png", (32, 32)),
        ("penumbra.png", (3840, 2160)),
        ("deckle.png", (3840, 2160)),
        ("museum-native.png", (3840, 2160)),
    ):
        with Image.open(name) as image:
            assert image.size == size
            arr = np.asarray(image)
            assert arr.min() > 0 and arr.max() < 255
