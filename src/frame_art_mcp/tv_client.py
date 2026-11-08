"""Synchronous Samsung adapter and cancellation-safe serialized device worker."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import fcntl
import logging
from pathlib import Path
import time
from typing import Any, Callable

import httpx
from samsungtvws.art import SamsungTVArt
from samsungtvws import exceptions
import websocket

from .discovery import Device
from .ledger import DeviceIOError, Engine, Ledger, TV, load_ledger

_LOG = logging.getLogger(__name__)
_REMOTE_ERRORS = (
    OSError,
    websocket.WebSocketException,
    exceptions.ConnectionFailure,
    exceptions.ResponseError,
    exceptions.MessageError,
)
# JSON decoding and shape errors originate inside the third-party Art API parser.
_API_ERRORS = _REMOTE_ERRORS + (KeyError, TypeError, ValueError)


class SamsungClient:
    def __init__(self, device: Device, directory: Path):
        self.device, self.directory = device, directory
        self.art: SamsungTVArt | None = None

    def probe(self, *, read_only: bool = False) -> tuple[str, str | None]:
        try:
            with httpx.Client(timeout=1.5, trust_env=False) as client:
                response = client.get(f"http://{self.device.ip}:8001/api/v2/")
                response.raise_for_status()
                dev = response.json()["device"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return "unknown", "http_error"
        if not isinstance(dev, dict):
            return "unknown", "invalid_device_metadata"
        if dev.get("duid") != self.device.duid or dev.get("FrameTVSupport") != "true":
            return "unknown", "duid_mismatch"
        if dev.get("PowerState") == "standby":
            return "off", None
        if dev.get("PowerState") != "on":
            return "unknown", "power_state_unknown"
        token_path = self.directory / "token.txt"
        try:
            token = token_path.read_text().strip()
        except FileNotFoundError:
            token = ""
        if read_only and dev.get("TokenAuthSupport") == "true" and not token:
            return "unknown", "pairing_required"
        try:
            if self.art is None:
                # The library never retries unauthorized tokens without a token.
                self.art = self._new_art(token, 3.0)
            self._timeout(3.0)
            mode = self.art.get_artmode()
            self._timeout(4.0)
            if not read_only and mode in ("on", "off") and not token:
                # Some Art channels authorize successfully without issuing a token.
                # Record that explicit write-path authorization for future inspection.
                if not token_path.exists() or not token_path.read_text().strip():
                    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    token_path.write_text("art-channel-authorized\n")
                    token_path.chmod(0o600)
            if mode == "on":
                return "art", None
            if mode == "off":
                return "tv", None
            return "unknown", "art_mode_unknown"
        except exceptions.UnauthorizedError:
            return "unknown", "pairing_required"
        except (*_API_ERRORS, DeviceIOError):
            return "unknown", "ws_error"

    def _new_art(self, token: str, timeout: float) -> SamsungTVArt:
        return SamsungTVArt(
            self.device.ip,
            port=8002,
            token=token or None,
            token_file=str(self.directory / "token.txt"),
            timeout=timeout,
            key_press_delay=0.15,
            name="Frame Art MCP",
        )

    def _timeout(self, seconds: float) -> None:
        assert self.art is not None
        self.art.timeout = seconds
        if self.art.connection is not None:
            try:
                self.art.connection.settimeout(seconds)
            except OSError as exc:
                raise DeviceIOError("ws_error") from exc

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        # Dynamic dispatch stays confined to the third-party, JSON-returning API.
        if self.art is None:
            raise DeviceIOError("art_connection_unavailable")
        try:
            try:
                return getattr(self.art, method)(*args, **kwargs)
            except _REMOTE_ERRORS:
                if method not in ("get_current", "available"):
                    raise
                timeout = self.art.timeout
                try:
                    self.art.close()
                except _REMOTE_ERRORS:
                    _LOG.debug("Discarding an already disconnected Art socket")
                self.art = None
                time.sleep(0.2)
                try:
                    token = (self.directory / "token.txt").read_text().strip()
                except FileNotFoundError:
                    token = ""
                self.art = self._new_art(token, timeout)
                return getattr(self.art, method)(*args, **kwargs)
        except _API_ERRORS as exc:
            raise DeviceIOError(type(exc).__name__) from exc

    def current(self) -> str:
        value = self._call("get_current")
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("content_id"), str)
            or not value["content_id"]
        ):
            raise DeviceIOError("invalid_current_artwork")
        return value["content_id"]

    def available(self) -> set[str]:
        value = self._call("available", "MY-C0002")
        if not isinstance(value, list) or any(
            not isinstance(v, dict) or not isinstance(v.get("content_id"), str)
            for v in value
        ):
            raise DeviceIOError("invalid_available_artwork")
        return {v["content_id"] for v in value}

    def upload(self, path: Path) -> str:
        self._timeout(15.0)
        try:
            return self._call(
                "upload",
                path.read_bytes(),
                matte="none",
                portrait_matte="none",
                file_type="jpg",
            )
        finally:
            self._timeout(4.0)

    def select(self, content_id: str) -> None:
        self._call("select_image", content_id, show=True)

    def delete(self, content_id: str) -> bool:
        return bool(self._call("delete", content_id))

    def close(self) -> None:
        if self.art is not None:
            self.art.close()
            self.art = None


class DeviceWorker:
    """One worker and OS lock per DUID; cancellation cannot release a running lock."""

    def __init__(
        self,
        directory: Path,
        duid: str,
        factory: Callable[[], TV],
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.directory, self.duid, self.factory = directory, duid, factory
        self.clock = clock
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="frame-device"
        )
        self.closed = False
        self._shutdown: asyncio.Task | None = None

    async def run(
        self,
        action: str,
        params: dict,
        request_id: str | None = None,
        params_hash: str | None = None,
    ) -> tuple[dict, Ledger]:
        if self.closed:
            raise RuntimeError("device_worker_closed")
        future = asyncio.get_running_loop().run_in_executor(
            self.executor, self._run, action, params, request_id, params_hash
        )
        return await asyncio.shield(future)

    def _run(
        self, action: str, params: dict, request_id: str | None, params_hash: str | None
    ) -> tuple[dict, Ledger]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with (self.directory / "ledger.lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                tv = self.factory()
                try:
                    ledger = load_ledger(self.directory, self.duid)
                    engine = Engine(self.directory, ledger, tv, self.clock)
                    response = engine.execute(action, params, request_id, params_hash)
                    return response, ledger
                finally:
                    try:
                        tv.close()
                    except _REMOTE_ERRORS:
                        _LOG.exception("Closing Samsung connection failed")
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            # Runs even when the awaiting coroutine was cancelled or request was cached.
            if action == "put":
                params["temp_path"].unlink(missing_ok=True)

    async def close(self) -> None:
        self.closed = True
        if self._shutdown is None:
            self._shutdown = asyncio.create_task(
                asyncio.to_thread(
                    self.executor.shutdown, wait=True, cancel_futures=False
                )
            )
        try:
            await asyncio.shield(self._shutdown)
        except asyncio.CancelledError:
            await self._shutdown
            raise
