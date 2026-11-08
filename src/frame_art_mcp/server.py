"""Four MCP mechanisms backed by per-device serialized transactions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .discovery import Device, Discovery, device_directory
from .guide import OPTICS_GUIDE
from .ledger import Ledger, SequenceStep, SLOT_NAME_PATTERN, TV
from .optics import Fit, Grade, condition
from .tv_client import DeviceWorker, SamsungClient

_LOG = logging.getLogger(__name__)


def valid_slot(slot: str) -> None:
    if not re.fullmatch(SLOT_NAME_PATTERN, slot):
        raise ValueError("invalid_slot")


class FrameService:
    def __init__(
        self,
        discovery: Discovery | None = None,
        client_factory: Callable[[Device, Path], TV] = SamsungClient,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.discovery = discovery if discovery is not None else Discovery()
        self.client_factory = client_factory
        self.clock, self.sleep = clock, sleep
        self.workers: dict[str, DeviceWorker] = {}
        self.devices: dict[str, Device] = {}
        self.states: dict[str, Ledger] = {}
        self.timers: dict[tuple[str, str, int], tuple[float, asyncio.Task]] = {}
        self.sequences: dict[str, tuple[int, asyncio.Task]] = {}
        self.transactions: set[asyncio.Task] = set()
        self.closing = False
        self.optical_lock = asyncio.Lock()
        self._shutdown: asyncio.Task | None = None

    async def _resolve(self, host: str | None) -> tuple[DeviceWorker | None, dict]:
        if self.closing:
            raise RuntimeError("server_closing")
        found = await self.discovery.resolve(host)
        if self.closing:
            raise RuntimeError("server_closing")
        dev = found.device or found.cached_device
        if dev is None:
            return None, {
                "status": found.status,
                "tv_reachable": False,
                "tv_mode": "unknown",
                "safe_to_project": False,
                "candidates": [asdict(c) for c in found.candidates],
            }
        self.devices[dev.duid] = dev
        if dev.duid not in self.workers:
            directory = device_directory(self.discovery.root, dev.duid)
            self.workers[dev.duid] = DeviceWorker(
                directory,
                dev.duid,
                lambda: self.client_factory(self.devices[dev.duid], directory),
                clock=self.clock,
            )
            # A previous process's sequence is historical intent, never resumable work.
            _, state = await self.workers[dev.duid].run("shutdown", {})
            self.states[dev.duid] = state
        info = {"tv_reachable": found.device is not None, "tv_info": asdict(dev)}
        if found.device is None:
            info["status"] = found.status
        return self.workers[dev.duid], info

    async def _submit(
        self,
        worker: DeviceWorker,
        action: str,
        params: dict,
        request_id: str | None = None,
        fingerprint: dict | None = None,
    ) -> dict:
        if self.closing:
            raise RuntimeError("server_closing")
        params_hash = (
            hashlib.sha256(
                json.dumps(
                    {
                        "action": action,
                        "params": fingerprint if fingerprint is not None else params,
                    },
                    sort_keys=True,
                    allow_nan=False,
                ).encode()
            ).hexdigest()
            if request_id
            else None
        )

        async def complete() -> dict:
            response, state = await worker.run(action, params, request_id, params_hash)
            self.states[worker.duid] = state
            if not self.closing:
                self._arm(worker, state)
            return response

        task = asyncio.create_task(complete())
        self.transactions.add(task)
        task.add_done_callback(self.transactions.discard)
        self._track_background(task)
        return await asyncio.shield(task)

    def _track_background(self, task: asyncio.Task) -> None:
        def finished(done: asyncio.Task) -> None:
            if not done.cancelled() and done.exception() is not None:
                _LOG.error("Frame background task failed", exc_info=done.exception())

        task.add_done_callback(finished)

    def _arm(self, worker: DeviceWorker, state: Ledger) -> None:
        deadlines = {
            (name, slot.revision): slot.expires_at
            for name, slot in state.slots.items()
            if slot.expires_at is not None
        }
        if state.displayed and state.displayed.expires_at is not None:
            deadlines[(state.displayed.slot, state.displayed.revision)] = (
                state.displayed.expires_at
            )
        for key, (_, task) in list(self.timers.items()):
            if key[0] == worker.duid and key[1:] not in deadlines:
                if task is not asyncio.current_task():
                    task.cancel()
                del self.timers[key]
        for (name, rev), exp in deadlines.items():
            key = (worker.duid, name, rev)
            previous = self.timers.get(key)
            if previous and previous[0] == exp:
                continue
            if previous:
                previous[1].cancel()
            if exp <= self.clock():
                continue

            async def expire_at(deadline: float, slot: str, revision: int) -> None:
                while (delay := deadline - self.clock()) > 0:
                    await self.sleep(delay)
                await self._submit(
                    worker, "expire", {"slot": slot, "revision": revision}
                )

            task = asyncio.create_task(expire_at(exp, name, rev))
            self.timers[key] = (exp, task)
            self._track_background(task)
        active = state.active_sequence
        previous_sequence = self.sequences.get(worker.duid)
        if previous_sequence and (
            not active or previous_sequence[0] != active["generation"]
        ):
            if previous_sequence[1] is not asyncio.current_task():
                previous_sequence[1].cancel()
            del self.sequences[worker.duid]
        if active and worker.duid not in self.sequences:
            task = asyncio.create_task(self._sequence(worker, active))
            self.sequences[worker.duid] = (active["generation"], task)
            self._track_background(task)

    async def _sequence(self, worker: DeviceWorker, active: dict) -> None:
        steps, generation = active["steps"], active["generation"]
        count = len(steps) * active["loop"]
        for index in range(count):
            await self.sleep(steps[index % len(steps)]["dwell_ms"] / 1000.0)
            if index + 1 < count:
                response = await self._submit(
                    worker,
                    "sequence",
                    {
                        "generation": generation,
                        "slot": steps[(index + 1) % len(steps)]["slot"],
                    },
                )
            else:
                response = await self._submit(
                    worker,
                    "sequence",
                    {
                        "generation": generation,
                        "slot": None,
                        "restore": active["restore_on_done"],
                    },
                )
            if response["status"] != "sequence_step":
                return

    async def inspect(
        self, include_optics_guide: bool = False, host: str | None = None
    ) -> dict:
        worker, info = await self._resolve(host)
        response = await self._submit(worker, "inspect", {}) if worker else {}
        return {
            **info,
            **response,
            "optics_guide": OPTICS_GUIDE if include_optics_guide else None,
        }

    async def put(
        self,
        image_path: str,
        slot: str = "main",
        show: bool = True,
        fit: Fit = "contain",
        optical_grade: Grade = "matte_natural",
        dry_run: bool = False,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
        ttl_sec: int | None = None,
        owner: str | None = None,
        request_id: str | None = None,
        host: str | None = None,
    ) -> dict:
        valid_slot(slot)
        if ttl_sec is not None and (
            type(ttl_sec) is not int or not 10 <= ttl_sec <= 86400
        ):
            raise ValueError("invalid_ttl_sec")
        # Validate JSON before allocating an image or entering a device transaction.
        json.dumps(metadata, allow_nan=False)
        worker, info = await self._resolve(host)
        if (worker is None or not info["tv_reachable"]) and not dry_run:
            return info
        directory = worker.directory if worker else self.discovery.root
        previous = self.states.get(worker.duid) if worker else None
        previous_path = (
            previous.displayed.cached_image_path
            if previous and previous.displayed
            else None
        )

        async def render():
            async with self.optical_lock:
                return await asyncio.to_thread(
                    condition,
                    image_path,
                    directory / "previews",
                    fit=fit,
                    optical_grade=optical_grade,
                    previous_path=previous_path,
                )

        rendering = asyncio.create_task(render())
        self.transactions.add(rendering)
        rendering.add_done_callback(self.transactions.discard)
        self._track_background(rendering)
        optical = await asyncio.shield(rendering)
        if self.closing:
            raise RuntimeError("server_closing")
        if dry_run:
            return {
                **info,
                **optical.result,
                "status": "dry_run",
                "projected": False,
                "staged": False,
                "slot": slot,
                "request_id": request_id,
            }
        assert worker is not None
        blobs = directory / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        temp_path = blobs / f"tmp_{os.getpid()}_{uuid4().hex}.jpg"
        with temp_path.open("xb") as stream:
            stream.write(optical.jpeg)
            stream.flush()
            os.fsync(stream.fileno())
        params = {
            "temp_path": temp_path,
            "slot": slot,
            "show": show,
            "ttl_sec": ttl_sec,
            "summary": summary,
            "metadata": metadata,
            "owner": owner,
            "optical": optical.result,
        }
        fingerprint = {
            "image_path": str(Path(image_path).resolve()),
            "image_sha256": hashlib.sha256(optical.jpeg).hexdigest(),
            "slot": slot,
            "show": show,
            "fit": fit,
            "optical_grade": optical_grade,
            "ttl_sec": ttl_sec,
            "summary": summary,
            "metadata": metadata,
            "owner": owner,
        }
        try:
            return {
                **info,
                **await self._submit(worker, "put", params, request_id, fingerprint),
            }
        except (TypeError, ValueError):
            # Parameter fingerprinting can fail before submission; cancellation is worker-owned.
            temp_path.unlink(missing_ok=True)
            raise

    async def switch(
        self,
        target: str | list[SequenceStep],
        loop: int = 1,
        restore_on_done: bool = False,
        request_id: str | None = None,
        host: str | None = None,
    ) -> dict:
        if type(loop) is not int or not 1 <= loop <= 100:
            raise ValueError("invalid_loop")
        if isinstance(target, str):
            valid_slot(target)
            normalized = target
        else:
            if not 1 <= len(target) <= 32:
                raise ValueError("invalid_sequence_length")
            normalized = [
                SequenceStep.model_validate(step).model_dump() for step in target
            ]
        worker, info = await self._resolve(host)
        return (
            {
                **info,
                **await self._submit(
                    worker,
                    "switch",
                    {
                        "target": normalized,
                        "loop": loop,
                        "restore_on_done": restore_on_done,
                    },
                    request_id,
                ),
            }
            if worker and info["tv_reachable"]
            else info
        )

    async def clear(
        self,
        slots: list[str] | None = None,
        restore_baseline: bool = True,
        request_id: str | None = None,
        host: str | None = None,
    ) -> dict:
        if slots is not None:
            for slot in slots:
                valid_slot(slot)
        worker, info = await self._resolve(host)
        return (
            {
                **info,
                **await self._submit(
                    worker,
                    "clear",
                    {"slots": slots, "restore_baseline": restore_baseline},
                    request_id,
                ),
            }
            if worker
            else info
        )

    async def close(self) -> None:
        self.closing = True
        if self._shutdown is None:
            self._shutdown = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self._shutdown)
        except asyncio.CancelledError:
            await self._shutdown
            raise

    async def _close(self) -> None:
        tasks = [task for _, task in self.timers.values()] + [
            task for _, task in self.sequences.values()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*list(self.transactions), return_exceptions=True)

        async def stop(worker: DeviceWorker) -> None:
            try:
                await worker.run("shutdown", {})
            finally:
                await worker.close()

        results = await asyncio.gather(
            *(stop(worker) for worker in self.workers.values()), return_exceptions=True
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("Device shutdown failed", errors)


_service: FrameService | None = None


def service() -> FrameService:
    global _service
    if _service is None:
        _service = FrameService()
    return _service


@asynccontextmanager
async def lifespan(mcp: FastMCP):
    try:
        yield
    finally:
        global _service
        if _service is not None:
            await _service.close()
            _service = None


mcp = FastMCP("frame-art-mcp", lifespan=lifespan)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def frame_inspect(
    include_optics_guide: bool = False, host: str | None = None
) -> dict[str, Any]:
    """Inspect mode, safety, baseline, managed slots and optional optical guide. Never writes to the TV or initiates pairing."""
    return await service().inspect(include_optics_guide, host)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def frame_put(
    image_path: str,
    slot: str = "main",
    show: bool = True,
    fit: Fit = "contain",
    optical_grade: Grade = "matte_natural",
    dry_run: bool = False,
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
    ttl_sec: int | None = None,
    owner: str | None = None,
    request_id: str | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Project or stage any local raster image (`JPG`, `PNG`, `WebP`, `TIFF`) onto a named slot on the 65" Samsung Frame TV (Matte Display).
    - **Safety**: Only projects when the TV is already on and in Art Mode (`tv_mode == "art"`). Never wakes a sleeping TV or interrupts TV/movie viewing.
    - **Two Usage Modes**:
      1. *Trompe-l'œil Wall Art / Ambient Signals*: Generate a `3840x2160` (`16:9`) image and use `optical_grade="matte_natural"` (default). Follow Matte Display optical laws: never draw fake outer frames or deep 3D shadowboxes; avoid large contiguous areas with `RGB < 22` (which glow gray on LCD); use 0.5–3mm tactile matte textures (impasto, lime plaster, rag paper, 1:1 macro crops of 8000px+ museum art, penumbra shadows); keep any text `<4%` of canvas area disguised as a physical label/seal. Use `dry_run=True` first and `read` the returned `512x512` `retinal_crops` to verify 1:1 pixel quality before uploading.
      2. *General 65-Inch Display ("大屏看图" for diagrams, charts, screenshots, photos)*: Pass any aspect ratio with `fit="contain"` (scales without cropping and pads with a matching paper/slate mat) and set `optical_grade="raw"` if exact UI/chart colors must be preserved (or `"matte_natural"` for soft paper-like reading in a dim room). Set `ttl_sec` (e.g. `900`) for temporary viewing so the wall automatically reverts afterward.
    """
    return await service().put(
        image_path,
        slot,
        show,
        fit,
        optical_grade,
        dry_run,
        summary,
        metadata,
        ttl_sec,
        owner,
        request_id,
        host,
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def frame_switch(
    target: str | list[SequenceStep],
    loop: int = 1,
    restore_on_done: bool = False,
    request_id: str | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Switch to a staged slot or run an in-session generation-bound timed sequence. Never wakes the TV."""
    return await service().switch(target, loop, restore_on_done, request_id, host)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
async def frame_clear(
    slots: list[str] | None = None,
    restore_baseline: bool = True,
    request_id: str | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Cancel sequences and clear only confirmed-owned images; evict the active image before deletion. Offline clears queue cleanup."""
    return await service().clear(slots, restore_baseline, request_id, host)


@mcp.resource("frame://guide/matte-optics")
def matte_optics() -> str:
    """Matte Display Optical Laws and four self-contained 15-line material recipes."""
    return OPTICS_GUIDE


def main() -> None:
    mcp.run(transport="stdio")
