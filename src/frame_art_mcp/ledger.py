"""Durable device state and safety-guarded write-path reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SLOT_NAME_PATTERN = r"[a-zA-Z0-9_-]{1,32}"


class StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Revision(StateModel):
    revision: int = Field(ge=1)
    content_id: str = Field(min_length=1)
    cached_image_path: str
    expires_at: float | None = None


class Slot(Revision):
    summary: str | None = None
    metadata: dict[str, Any] = Field(
        default_factory=dict
    )  # Arbitrary caller JSON metadata.
    owner: str | None = None
    created_at: str
    optical_diagnostics: dict[str, Any] = Field(default_factory=dict)


class Displayed(Revision):
    slot: str


class History(StateModel):
    slot: str
    revision: int
    content_id: str


class SequenceStep(StateModel):
    slot: str = Field(pattern=f"^{SLOT_NAME_PATTERN}$")
    dwell_ms: int = Field(ge=250, le=3_600_000)


class SequenceState(StateModel):
    generation: int = Field(ge=0)
    steps: list[SequenceStep] = Field(min_length=1, max_length=32)
    loop: int = Field(ge=1, le=100)
    restore_on_done: bool


class UploadRecord(StateModel):
    op: Literal["upload"]
    slot: str = Field(pattern=f"^{SLOT_NAME_PATTERN}$")
    pre_ids: list[str]
    request_id: str | None


class SelectRecord(StateModel):
    op: Literal["select"]
    slot: str | None
    revision: int | None
    target_id: str = Field(min_length=1)
    cached_image_path: str | None
    pre_select_id: str = Field(min_length=1)
    expires_at: float | None

    @model_validator(mode="after")
    def validate_revision(self) -> SelectRecord:
        if self.slot is not None:
            if (
                not re.fullmatch(SLOT_NAME_PATTERN, self.slot)
                or self.revision is None
                or self.revision < 1
                or self.cached_image_path is None
            ):
                raise ValueError("invalid_managed_select")
        elif self.revision is not None or self.cached_image_path is not None:
            raise ValueError("invalid_baseline_select")
        return self


class RequestRecord(StateModel):
    params_hash: str
    status: str
    updated_at: float
    response: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> RequestRecord:
        if (
            self.status not in ("in_progress", "outcome_unknown")
            and self.response is None
        ):
            raise ValueError("missing_terminal_request_response")
        return self


class Ledger(StateModel):
    schema_version: Literal[3] = 3
    duid: str
    control_generation: int = Field(default=0, ge=0)
    next_revision: int = Field(default=1, ge=1)
    baseline_content_id: str | None = None
    expected_displayed_id: str | None = None
    displayed: Displayed | None = None
    display_history: list[History] = Field(default_factory=list)
    user_override_detected: bool = False
    pending_baseline_restore: bool = False
    active_sequence: dict[str, Any] | None = None
    slots: dict[str, Slot] = Field(default_factory=dict)
    owned_ids: list[str] = Field(default_factory=list)
    pending_delete_ids: list[str] = Field(default_factory=list)
    unattributed_candidates: list[str] = Field(default_factory=list)
    in_flight_op: dict[str, Any] | None = None
    recent_requests: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_state(self) -> Ledger:
        if self.in_flight_op is not None:
            wal = self.in_flight_op
            if wal.get("op") == "upload":
                UploadRecord.model_validate(wal)
            elif wal.get("op") == "select":
                SelectRecord.model_validate(wal)
                if wal["slot"] is not None and wal["target_id"] not in self.owned_ids:
                    raise ValueError("unowned_select_target")
            else:
                raise ValueError("invalid_write_ahead_record")
        if self.active_sequence is not None:
            SequenceState.model_validate(self.active_sequence)
        for request in self.recent_requests.values():
            RequestRecord.model_validate(request)
        if any(not re.fullmatch(SLOT_NAME_PATTERN, name) for name in self.slots):
            raise ValueError("invalid_slot_name")
        if not set(self.pending_delete_ids) <= set(self.owned_ids):
            raise ValueError("unowned_pending_delete")
        if any(s.content_id not in self.owned_ids for s in self.slots.values()):
            raise ValueError("unowned_slot")
        if self.displayed and self.displayed.content_id not in self.owned_ids:
            raise ValueError("unowned_displayed_revision")
        revisions = [s.revision for s in self.slots.values()]
        if self.displayed:
            revisions.append(self.displayed.revision)
        if revisions and self.next_revision <= max(revisions):
            raise ValueError("non_monotonic_revision")
        return self


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w") as stream:
            json.dump(value, stream, allow_nan=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fsync_directory(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def load_ledger(directory: Path, duid: str) -> Ledger:
    path = directory / "ledger.json"
    try:
        ledger = Ledger.model_validate_json(path.read_bytes())
        if ledger.duid != duid:
            raise ValueError("ledger_duid_mismatch")
    except FileNotFoundError:
        ledger = Ledger(duid=duid)
    except (ValidationError, ValueError):
        path.rename(directory / f"ledger.corrupt.{time.time_ns()}.json")
        fsync_directory(directory)
        ledger = Ledger(duid=duid)
    # An install may have reached disk before its ledger commit. Never reuse its revision.
    for blob in (directory / "blobs").glob("*__r*.jpg"):
        match = re.fullmatch(rf"{SLOT_NAME_PATTERN}__r([1-9][0-9]*)\.jpg", blob.name)
        if match:
            ledger.next_revision = max(ledger.next_revision, int(match[1]) + 1)
    return ledger


class TV(Protocol):
    def probe(self, *, read_only: bool = False) -> tuple[str, str | None]: ...
    def current(self) -> str: ...
    def available(self) -> set[str]: ...
    def upload(self, path: Path) -> str: ...
    def select(self, content_id: str) -> None: ...
    def delete(self, content_id: str) -> bool: ...
    def close(self) -> None: ...


class DeviceIOError(Exception):
    """A remote operation failed; its side effects may be unknown."""


class Engine:
    def __init__(
        self,
        directory: Path,
        ledger: Ledger,
        tv: TV,
        clock: Callable[[], float] = time.time,
    ):
        self.directory, self.state, self.tv, self.clock = directory, ledger, tv, clock
        self.mode = "unknown"
        self.reason: str | None = None
        self.current_id: str | None = None
        self.deleted: list[str] = []
        self.restored: str | None = None

    def save(self) -> None:
        atomic_json(self.directory / "ledger.json", self.state.model_dump())

    def observe(self, *, read_only: bool = False) -> bool:
        self.mode, self.reason = self.tv.probe(read_only=read_only)
        if self.mode != "art":
            if self.state.active_sequence:
                self.state.control_generation += 1
                self.state.active_sequence = None
                self.save()
            return False
        self.current_id = self.tv.current()
        s = self.state
        wal = s.in_flight_op
        if wal and wal["op"] == "upload":
            candidates = self.tv.available() - set(wal["pre_ids"]) - set(s.owned_ids)
            s.unattributed_candidates = sorted(
                set(s.unattributed_candidates) | candidates
            )
            s.in_flight_op = None
        elif wal and wal["op"] == "select":
            if self.current_id == wal["target_id"]:
                self._commit_selection(wal)
            elif self.current_id == wal["pre_select_id"]:
                s.expected_displayed_id = self.current_id
                s.in_flight_op = None
            else:
                # A third image proves the interrupted operation lost control.
                s.expected_displayed_id = wal["target_id"]
                s.in_flight_op = None
        if (
            s.expected_displayed_id is not None
            and self.current_id != s.expected_displayed_id
        ):
            self.override()
        if s.expected_displayed_id is None and self.current_id not in s.owned_ids:
            s.baseline_content_id = self.current_id
        self.save()
        return True

    def override(self) -> None:
        s = self.state
        s.user_override_detected = True
        s.expected_displayed_id = None
        s.displayed = None
        s.pending_baseline_restore = False
        s.active_sequence = None
        s.control_generation += 1
        if self.current_id not in s.owned_ids:
            s.baseline_content_id = self.current_id

    def _commit_selection(self, wal: dict) -> None:
        s = self.state
        if wal["slot"] is None:
            s.displayed = None
            s.expected_displayed_id = None
            s.baseline_content_id = wal["target_id"]
        else:
            s.displayed = Displayed(
                slot=wal["slot"],
                revision=wal["revision"],
                content_id=wal["target_id"],
                cached_image_path=wal["cached_image_path"],
                expires_at=wal["expires_at"],
            )
            s.expected_displayed_id = wal["target_id"]
            s.user_override_detected = False
            s.display_history.append(
                History(
                    slot=wal["slot"],
                    revision=wal["revision"],
                    content_id=wal["target_id"],
                )
            )
            slot = s.slots.get(wal["slot"])
            if slot and slot.revision == wal["revision"]:
                slot.expires_at = wal["expires_at"]
        s.pending_baseline_restore = False
        s.in_flight_op = None

    def select(
        self,
        slot: str | None,
        entry: Revision | None,
        target_id: str,
        ttl_sec: int | None = None,
    ) -> None:
        wal = {
            "op": "select",
            "slot": slot,
            "revision": entry.revision if entry else None,
            "target_id": target_id,
            "cached_image_path": entry.cached_image_path if entry else None,
            "pre_select_id": self.current_id,
            "expires_at": (self.clock() + ttl_sec)
            if ttl_sec
            else (entry.expires_at if entry else None),
        }
        self.state.in_flight_op = wal
        self.save()
        self.tv.select(target_id)
        if self.tv.current() != target_id:
            raise DeviceIOError("select_verification_failed")
        self.current_id = target_id
        if ttl_sec:
            wal["expires_at"] = self.clock() + ttl_sec
        self._commit_selection(wal)
        self.save()

    def queue_delete(self, content_id: str) -> None:
        if (
            content_id in self.state.owned_ids
            and content_id not in self.state.pending_delete_ids
        ):
            self.state.pending_delete_ids.append(content_id)

    def expire(self) -> None:
        s, now = self.state, self.clock()
        for name, slot in list(s.slots.items()):
            if slot.expires_at is not None and now >= slot.expires_at:
                del s.slots[name]
                self._forget_revision(name, slot)
        if (
            s.displayed
            and s.displayed.expires_at is not None
            and now >= s.displayed.expires_at
        ):
            self._forget_revision(s.displayed.slot, s.displayed)
        self.save()

    def _forget_revision(self, name: str, entry: Revision) -> None:
        self.queue_delete(entry.content_id)
        s = self.state
        s.display_history = [
            h
            for h in s.display_history
            if (h.slot, h.revision) != (name, entry.revision)
        ]
        if s.displayed and (s.displayed.slot, s.displayed.revision) == (
            name,
            entry.revision,
        ):
            s.pending_baseline_restore = True

    def collect(self) -> None:
        s = self.state
        if s.pending_baseline_restore and not s.user_override_detected:
            if not self.observe():
                return
            if s.pending_baseline_restore and self.current_id in s.pending_delete_ids:
                fallback = next(
                    (
                        h
                        for h in reversed(s.display_history)
                        if h.slot in s.slots
                        and s.slots[h.slot].revision == h.revision
                        and (
                            s.slots[h.slot].expires_at is None
                            or s.slots[h.slot].expires_at > self.clock()
                        )
                        and h.content_id not in s.pending_delete_ids
                    ),
                    None,
                )
                target_id = fallback.content_id if fallback else s.baseline_content_id
                if target_id and target_id != self.current_id:
                    entry = s.slots[fallback.slot] if fallback else None
                    self.select(fallback.slot if fallback else None, entry, target_id)
                    self.restored = target_id
        for del_id in list(s.pending_delete_ids):
            if not self.observe():
                break
            referenced = {
                s.baseline_content_id,
                self.current_id,
                s.expected_displayed_id,
            }
            referenced.update(entry.content_id for entry in s.slots.values())
            if s.displayed:
                referenced.add(s.displayed.content_id)
            if del_id in s.owned_ids and del_id not in referenced:
                assert del_id in s.owned_ids and del_id != s.baseline_content_id
                confirmed = self.tv.delete(del_id)
                if confirmed or del_id not in self.tv.available():
                    s.pending_delete_ids.remove(del_id)
                    s.owned_ids.remove(del_id)
                    self.deleted.append(del_id)
                    self.save()
        self._prune_blobs()
        self.save()

    def _prune_blobs(self) -> None:
        # Keep staged/displayed caches and defer pruning while remote deletes are pending.
        referenced = {Path(e.cached_image_path) for e in self.state.slots.values()}
        if self.state.displayed:
            referenced.add(Path(self.state.displayed.cached_image_path))
        # Pending IDs may still require their immutable cache after a failed eviction.
        if not self.state.pending_delete_ids:
            for path in (self.directory / "blobs").glob("*__r*.jpg"):
                if path not in referenced:
                    path.unlink(missing_ok=True)

    def reconcile(self) -> bool:
        if not self.observe():
            return False
        self.expire()
        self.collect()
        return self.mode == "art"

    def snapshot(self) -> dict:
        s = self.state
        return {
            "tv_mode": self.mode,
            "safe_to_project": self.mode == "art",
            "reason": self.reason,
            "observed_current_id": self.current_id,
            "control_generation": s.control_generation,
            "baseline_content_id": s.baseline_content_id,
            "expected_displayed_id": s.expected_displayed_id,
            "displayed": s.displayed.model_dump() if s.displayed else None,
            "slots": {k: v.model_dump() for k, v in s.slots.items()},
            "user_override_detected": s.user_override_detected,
            "pending_baseline_restore": s.pending_baseline_restore,
            "active_sequence": s.active_sequence,
            "owned_ids_count": len(s.owned_ids),
            "pending_delete_ids": list(s.pending_delete_ids),
            "unattributed_candidates": list(s.unattributed_candidates),
        }

    def put(
        self,
        *,
        temp_path: Path,
        slot: str,
        show: bool,
        ttl_sec: int | None,
        summary: str | None,
        metadata: dict | None,
        owner: str | None,
        optical: dict,
    ) -> dict:
        s = self.state
        if not self.reconcile():
            return {"status": "blocked_by_tv_mode", "projected": False, "staged": False}
        pre_id = self.current_id
        s.in_flight_op = {
            "op": "upload",
            "slot": slot,
            "pre_ids": sorted(self.tv.available()),
            "request_id": self.request_id,
        }
        self.save()
        new_id = self.tv.upload(temp_path)
        if not isinstance(new_id, str) or not new_id:
            raise DeviceIOError("invalid_upload_content_id")
        rev = s.next_revision
        s.next_revision += 1
        blob = self.directory / "blobs" / f"{slot}__r{rev}.jpg"
        os.replace(temp_path, blob)
        fsync_directory(blob.parent)
        if new_id not in s.owned_ids:
            s.owned_ids.append(new_id)
        if slot in s.slots and s.slots[slot].content_id != new_id:
            self.queue_delete(s.slots[slot].content_id)
        s.slots[slot] = Slot(
            revision=rev,
            content_id=new_id,
            cached_image_path=str(blob),
            expires_at=self.clock() + ttl_sec if ttl_sec else None,
            summary=summary,
            metadata=metadata or {},
            owner=owner,
            created_at=datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
            optical_diagnostics=optical["optical_diagnostics"],
        )
        s.in_flight_op = None
        self.save()
        status = "staged"
        if show:
            s.control_generation += 1
            s.active_sequence = None
            self.mode, self.reason = self.tv.probe()
            if self.mode != "art":
                status = "staged_only_tv_left_art_mode"
            else:
                self.current_id = self.tv.current()
                if self.current_id != pre_id:
                    self.override()
                    status = "staged_only_user_override_during_upload"
                else:
                    self.select(slot, s.slots[slot], new_id, ttl_sec)
                    status = "projected"
        self.save()
        self.collect()
        return {
            **optical,
            "status": status,
            "projected": status == "projected",
            "staged": True,
            "slot": slot,
            "revision": rev,
            "content_id": new_id,
            "cached_image_path": str(blob),
            "pending_delete_count": len(s.pending_delete_ids),
        }

    def switch(
        self, *, target: str | list[dict], loop: int, restore_on_done: bool
    ) -> dict:
        if not self.observe():
            return {"status": "blocked_by_tv_mode"}
        # Diagnose expiration before the reconciliation sweep removes the slot.
        names = (
            [target] if isinstance(target, str) else [step["slot"] for step in target]
        )
        if any(
            name in self.state.slots
            and self.state.slots[name].expires_at is not None
            and self.state.slots[name].expires_at <= self.clock()
            for name in names
        ):
            self.expire()
            self.collect()
            return {"status": "slot_expired"}
        if not self.reconcile():
            return {"status": "blocked_by_tv_mode"}
        if any(name not in self.state.slots for name in names):
            return {"status": "slot_not_found"}
        s = self.state
        s.control_generation += 1
        s.active_sequence = None
        self.save()
        name = names[0]
        self.select(name, s.slots[name], s.slots[name].content_id)
        self.collect()
        if isinstance(target, list):
            if self.mode != "art":
                return {"status": "blocked_by_tv_mode"}
            if s.expected_displayed_id != s.slots[name].content_id:
                return {"status": "outcome_unknown"}
            s.active_sequence = {
                "generation": s.control_generation,
                "steps": target,
                "loop": loop,
                "restore_on_done": restore_on_done,
            }
            self.save()
            return {"status": "sequence_started"}
        return {"status": "switched"}

    def clear(self, *, slots: list[str] | None, restore_baseline: bool) -> dict:
        s = self.state
        # Offline clears still alter local intent, never device state.
        self.observe()
        if self.mode == "art":
            self.expire()
        s.control_generation += 1
        s.active_sequence = None
        names = list(s.slots) if slots is None else slots
        cleared: list[str] = []
        for name in names:
            entry = s.slots.pop(name, None)
            if entry:
                cleared.append(name)
                self._forget_revision(name, entry)
        # An old displayed revision may have been superseded by show=False.
        if s.displayed and (slots is None or s.displayed.slot in names):
            self._forget_revision(s.displayed.slot, s.displayed)
        if not restore_baseline:
            s.pending_baseline_restore = False
        self.save()
        if self.mode == "art":
            self.collect()
        status = (
            "deferred_offline_clear"
            if self.mode != "art"
            else (
                "cleanup_pending_active_display" if s.pending_delete_ids else "cleared"
            )
        )
        missing = set(names) - set(cleared)
        if missing and status == "cleared":
            status = "partial_clear"
        return {
            "status": status,
            "cleared_slots": cleared,
            "restored_to_content_id": self.restored,
            "deleted_content_ids": self.deleted,
        }

    def sequence_step(
        self, *, generation: int, slot: str | None, restore: bool = False
    ) -> dict:
        s = self.state
        if s.control_generation != generation or not s.active_sequence:
            return {"status": "sequence_cancelled"}
        if (
            not self.reconcile()
            or s.control_generation != generation
            or not s.active_sequence
        ):
            return {"status": "sequence_cancelled"}
        if (
            s.expected_displayed_id is None
            or self.current_id != s.expected_displayed_id
        ):
            s.active_sequence = None
            self.save()
            return {"status": "sequence_cancelled"}
        if restore:
            if s.baseline_content_id:
                self.select(None, None, s.baseline_content_id)
            s.active_sequence = None
        elif slot is not None:
            entry = s.slots.get(slot)
            if entry is None or (
                entry.expires_at is not None and entry.expires_at <= self.clock()
            ):
                s.active_sequence = None
                self.save()
                return {"status": "slot_expired"}
            self.select(slot, entry, entry.content_id)
        else:
            s.active_sequence = None
        self.collect()
        self.save()
        return {"status": "sequence_step"}

    def execute(
        self,
        action: str,
        params: dict,
        request_id: str | None = None,
        params_hash: str | None = None,
    ) -> dict:
        self.request_id = request_id
        s = self.state
        try:
            if action == "inspect":
                self.observe(read_only=True)
                return self.snapshot()
            cached = s.recent_requests.get(request_id) if request_id else None
            if cached:
                if cached["params_hash"] != params_hash:
                    return {
                        "status": "request_id_conflict",
                        **self.snapshot(),
                        "request_id": request_id,
                        "duid": s.duid,
                    }
                if cached["status"] not in ("in_progress", "outcome_unknown"):
                    return cached["response"]
                self.reconcile()
                # Reconciliation can establish ownership/selection, but never replays a write.
                response = {
                    "status": "outcome_unknown",
                    **self.snapshot(),
                    "request_id": request_id,
                    "duid": s.duid,
                }
                cached.update(
                    status="outcome_unknown", response=response, updated_at=self.clock()
                )
                self.save()
                return response
            if request_id:
                s.recent_requests[request_id] = {
                    "params_hash": params_hash,
                    "status": "in_progress",
                    "updated_at": self.clock(),
                }
                self.save()
            if action == "put":
                result = self.put(**params)
            elif action == "switch":
                result = self.switch(**params)
            elif action == "clear":
                result = self.clear(**params)
            elif action == "expire":
                slot = s.slots.get(params["slot"])
                displayed = s.displayed
                if (slot and slot.revision == params["revision"]) or (
                    displayed
                    and (displayed.slot, displayed.revision)
                    == (params["slot"], params["revision"])
                ):
                    self.reconcile()
                result = {"status": "expiry_checked"}
            elif action == "sequence":
                result = self.sequence_step(**params)
            elif action == "shutdown":
                s.active_sequence = None
                s.control_generation += 1
                self.save()
                result = {"status": "stopped"}
            else:
                raise ValueError("unknown_worker_action")
            response = {
                **result,
                **self.snapshot(),
                "request_id": request_id,
                "duid": s.duid,
            }
        except DeviceIOError as exc:
            response = {
                **self.snapshot(),
                "status": "outcome_unknown",
                "reason": str(exc),
                "request_id": request_id,
                "duid": s.duid,
            }
        if request_id:
            s.recent_requests[request_id].update(
                status=response["status"], response=response, updated_at=self.clock()
            )
            self.save()
        return response
