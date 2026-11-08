import asyncio
import fcntl
import json
import os
from pathlib import Path
import threading

import pytest

from frame_art_mcp.ledger import Engine, Ledger, atomic_json, load_ledger
from frame_art_mcp.tv_client import DeviceWorker


@pytest.fixture
def engine(tmp_path, tv):
    (tmp_path / "blobs").mkdir()
    return Engine(tmp_path, Ledger(duid="uuid:test"), tv, clock=lambda: 1000.0)


def put(engine, slot="main", show=True, ttl=None, request_id=None, fingerprint="same"):
    path = engine.directory / "blobs" / "temp.jpg"
    path.write_bytes(b"jpeg-payload")
    return engine.execute(
        "put",
        {
            "temp_path": path,
            "slot": slot,
            "show": show,
            "ttl_sec": ttl,
            "summary": "sample",
            "metadata": {"kind": "test"},
            "owner": None,
            "optical": {"optical_diagnostics": {"test": 1}},
        },
        request_id,
        fingerprint,
    )


@pytest.mark.parametrize("mode", ["tv", "off", "unknown"])
def test_all_writes_closed_outside_art(engine, tv, mode):
    tv.mode = mode
    result = put(engine)
    assert result["status"] == "blocked_by_tv_mode"
    assert (result["projected"], result["staged"]) == (False, False)
    assert (
        engine.execute(
            "switch", {"target": "main", "loop": 1, "restore_on_done": False}
        )["status"]
        == "blocked_by_tv_mode"
    )
    result = engine.execute("clear", {"slots": None, "restore_baseline": True})
    assert result["status"] == "deferred_offline_clear"
    assert not any(c[0] in ("upload", "select", "delete") for c in tv.calls)


def test_stage_superseding_display_keeps_old_until_selected(engine, tv):
    first = put(engine, ttl=20)
    old = first["content_id"]
    second = put(engine, show=False, ttl=50)
    new = second["content_id"]
    assert first["status"] == "projected" and second["status"] == "staged"
    assert engine.state.slots["main"].revision == 2
    assert engine.state.displayed.revision == 1
    assert engine.state.displayed.expires_at == 1020.0
    assert old in engine.state.pending_delete_ids and old in tv.images
    switched = engine.execute(
        "switch", {"target": "main", "loop": 1, "restore_on_done": False}
    )
    assert switched["status"] == "switched"
    engine.reconcile()
    assert tv.current_id == new and old not in tv.images
    assert tv.calls.index(("select", new)) < tv.calls.index(("delete", old))
    assert "USER_BASE" in tv.images


def test_ttl_expiry_of_superseded_display_restores_newest_valid_history(engine, tv):
    put(engine, slot="fallback")
    first = put(engine, ttl=10)
    newer = put(engine, show=False, ttl=60)
    engine.clock = lambda: 1011.0
    engine.reconcile()
    assert engine.state.slots["main"].content_id == newer["content_id"]
    assert engine.state.displayed.slot == "fallback"
    assert tv.current_id == engine.state.slots["fallback"].content_id
    assert first["content_id"] not in tv.images


def test_expired_history_is_filtered_before_baseline_fallback(engine, tv):
    first = put(engine, ttl=10)
    engine.clock = lambda: 1010.0
    engine.reconcile()
    assert tv.current_id == "USER_BASE"
    assert engine.state.displayed is None
    assert engine.state.expected_displayed_id is None
    assert engine.state.slots == {}
    assert first["content_id"] not in tv.images
    assert not engine.state.pending_delete_ids


@pytest.mark.parametrize("restore", [False, True])
def test_offline_clear_defers_effects_and_later_converges(engine, tv, restore):
    first = put(engine)
    tv.mode = "off"
    before = [c for c in tv.calls if c[0] in ("select", "delete")]
    result = engine.execute("clear", {"slots": None, "restore_baseline": restore})
    assert result["status"] == "deferred_offline_clear"
    assert (
        engine.state.slots == {}
        and first["content_id"] in engine.state.pending_delete_ids
    )
    assert [c for c in tv.calls if c[0] in ("select", "delete")] == before
    tv.mode = "art"
    engine.reconcile()
    if restore:
        assert tv.current_id == "USER_BASE" and first["content_id"] not in tv.images
    else:
        assert tv.current_id == first["content_id"] and first["content_id"] in tv.images


def test_human_override_preserves_slots_and_user_art(engine, tv):
    first = put(engine)
    gen = engine.state.control_generation
    engine.state.active_sequence = {"generation": gen}
    tv.current_id = "USER_PHOTO"
    response = engine.execute("inspect", {})
    assert response["user_override_detected"] is True
    assert response["control_generation"] == gen + 1
    assert response["displayed"] is None and response["active_sequence"] is None
    assert engine.state.baseline_content_id == "USER_PHOTO"
    assert engine.state.slots["main"].content_id == first["content_id"]
    assert not any(c[0] == "delete" for c in tv.calls)


def test_override_to_owned_image_is_still_human_override(engine, tv):
    one = put(engine, slot="one")
    put(engine, slot="two")
    tv.current_id = one["content_id"]
    engine.execute("inspect", {})
    assert engine.state.user_override_detected
    assert engine.state.baseline_content_id == "USER_BASE"
    assert set(engine.state.slots) == {"one", "two"}


@pytest.mark.parametrize(
    "change,status",
    [
        ("mode", "staged_only_tv_left_art_mode"),
        ("image", "staged_only_user_override_during_upload"),
    ],
)
def test_second_upload_gate(engine, tv, change, status):
    def during_upload(tv):
        if change == "mode":
            tv.mode = "tv"
        else:
            tv.current_id = "USER_PHOTO"

    tv.after_upload = during_upload
    response = put(engine)
    assert response["status"] == status
    assert response["staged"] is True and response["projected"] is False
    assert response["content_id"] in engine.state.owned_ids
    assert not any(c[0] == "select" for c in tv.calls)


def test_upload_lost_reply_only_creates_unattributed_candidate(engine, tv):
    tv.upload_error = True
    result = put(engine, request_id="request")
    assert result["status"] == "outcome_unknown"
    assert engine.state.in_flight_op["op"] == "upload"
    candidate = next(cid for cid in tv.images if cid.startswith("MY_F"))
    tv.images.add("USER_CONCURRENT_UPLOAD")
    tv.upload_error = False
    retry = put(engine, request_id="request")
    assert retry["status"] == "outcome_unknown"
    assert set(engine.state.unattributed_candidates) == {
        candidate,
        "USER_CONCURRENT_UPLOAD",
    }
    assert not engine.state.owned_ids and not engine.state.pending_delete_ids
    assert len([c for c in tv.calls if c[0] == "upload"]) == 1
    assert candidate in tv.images


@pytest.mark.parametrize("baseline", [True, False])
def test_interrupted_select_recovers_baseline_and_managed_revision(
    engine, tv, baseline
):
    first = put(engine)
    tv.select_error = True
    if baseline:
        result = engine.execute(
            "clear", {"slots": None, "restore_baseline": True}, "clear", "clearhash"
        )
    else:
        result = put(engine, request_id="put")
    assert result["status"] == "outcome_unknown"
    saved = load_ledger(engine.directory, "uuid:test")
    assert saved.in_flight_op["op"] == "select"
    recovered = Engine(engine.directory, saved, tv, clock=lambda: 1001.0)
    tv.select_error = False
    recovered.reconcile()
    assert saved.in_flight_op is None
    assert saved.pending_baseline_restore is False
    if baseline:
        assert saved.displayed is None and saved.expected_displayed_id is None
        assert saved.baseline_content_id == "USER_BASE"
        assert first["content_id"] not in tv.images
    else:
        assert saved.displayed.content_id == tv.current_id
        assert saved.expected_displayed_id == tv.current_id
        assert saved.displayed.revision == 2


@pytest.mark.parametrize("observed", ["pre", "third"])
def test_select_recovery_no_effect_or_third_image(engine, tv, observed):
    first = put(engine)
    engine.state.in_flight_op = {
        "op": "select",
        "slot": "main",
        "revision": 1,
        "target_id": first["content_id"],
        "cached_image_path": first["cached_image_path"],
        "pre_select_id": "USER_BASE",
        "expires_at": None,
    }
    tv.current_id = "USER_BASE" if observed == "pre" else "USER_PHOTO"
    engine.observe()
    assert engine.state.in_flight_op is None
    if observed == "pre":
        assert engine.state.expected_displayed_id == "USER_BASE"
        assert not engine.state.user_override_detected
    else:
        assert (
            engine.state.user_override_detected
            and engine.state.baseline_content_id == "USER_PHOTO"
        )


def test_per_delete_guard_stops_after_mode_change(engine, tv):
    one = put(engine, "one", show=False)
    two = put(engine, "two", show=False)
    tv.before_delete = lambda device: setattr(device, "mode", "tv")
    result = engine.execute("clear", {"slots": None, "restore_baseline": True})
    assert len(result["deleted_content_ids"]) == 1
    remaining = {one["content_id"], two["content_id"]} - set(
        result["deleted_content_ids"]
    )
    assert set(engine.state.pending_delete_ids) == remaining
    assert remaining <= tv.images


def test_unconfirmed_delete_remains_owned_and_pending(engine, tv):
    first = put(engine, show=False)
    tv.delete_confirmed = False
    engine.execute("clear", {"slots": None, "restore_baseline": True})
    assert first["content_id"] in engine.state.owned_ids
    assert first["content_id"] in engine.state.pending_delete_ids


def test_request_id_conflict_and_terminal_replay(engine, tv):
    first = put(engine, request_id="id")
    replay = put(engine, request_id="id")
    conflict = put(engine, request_id="id", fingerprint="different")
    assert replay == first
    assert conflict["status"] == "request_id_conflict"
    assert len([c for c in tv.calls if c[0] == "upload"]) == 1


def test_expired_switch_target_is_distinct_from_missing(engine, tv):
    put(engine, show=False, ttl=10)
    engine.clock = lambda: 1010.0
    result = engine.execute(
        "switch", {"target": "main", "loop": 1, "restore_on_done": False}
    )
    assert result["status"] == "slot_expired"
    assert not any(c[0] == "select" for c in tv.calls)


def test_sequence_generation_and_mode_stop(engine, tv):
    put(engine, "a", show=False)
    put(engine, "b", show=False)
    result = engine.execute(
        "switch",
        {
            "target": [{"slot": "a", "dwell_ms": 250}, {"slot": "b", "dwell_ms": 250}],
            "loop": 1,
            "restore_on_done": True,
        },
    )
    gen = result["control_generation"]
    assert result["status"] == "sequence_started"
    engine.execute("sequence", {"generation": gen, "slot": "b"})
    assert tv.current_id == engine.state.slots["b"].content_id
    tv.mode = "tv"
    result = engine.execute("sequence", {"generation": gen, "slot": "a"})
    assert result["status"] == "sequence_cancelled"
    assert engine.state.active_sequence is None
    tv.mode = "art"
    result = engine.execute(
        "sequence", {"generation": gen, "slot": None, "restore": True}
    )
    assert result["status"] == "sequence_cancelled"
    assert tv.current_id != "USER_BASE"


def test_sequence_stops_when_expiry_eviction_releases_expected_display(engine, tv):
    put(engine, "a", show=False, ttl=10)
    put(engine, "b", show=False)
    result = engine.execute(
        "switch",
        {
            "target": [{"slot": "a", "dwell_ms": 250}, {"slot": "b", "dwell_ms": 250}],
            "loop": 1,
            "restore_on_done": True,
        },
    )
    engine.clock = lambda: 1011.0
    response = engine.execute(
        "sequence", {"generation": result["control_generation"], "slot": "b"}
    )
    assert response["status"] == "sequence_cancelled"
    assert engine.state.active_sequence is None
    assert tv.current_id == "USER_BASE"
    assert "b" in engine.state.slots
    assert ("select", engine.state.slots["b"].content_id) not in tv.calls


def test_sequence_step_collects_superseded_display_after_switch(engine, tv):
    old = put(engine, "a", show=False)
    put(engine, "b", show=False)
    result = engine.execute(
        "switch",
        {
            "target": [{"slot": "a", "dwell_ms": 250}, {"slot": "b", "dwell_ms": 250}],
            "loop": 1,
            "restore_on_done": False,
        },
    )
    newer = put(engine, "a", show=False)
    assert old["content_id"] in tv.images
    response = engine.execute(
        "sequence", {"generation": result["control_generation"], "slot": "b"}
    )
    assert response["status"] == "sequence_step"
    assert old["content_id"] not in tv.images
    assert newer["content_id"] in tv.images
    assert tv.current_id == engine.state.slots["b"].content_id


@pytest.mark.parametrize(
    "payload",
    [b"{bad", b'{"schema_version": 99}', b'{"schema_version": 3, "duid": "wrong"}'],
)
def test_corrupt_or_incompatible_ledger_quarantines_without_ownership(
    tmp_path, payload
):
    (tmp_path / "ledger.json").write_bytes(payload)
    ledger = load_ledger(tmp_path, "uuid:test")
    assert ledger.owned_ids == [] and ledger.pending_delete_ids == []
    quarantined = list(tmp_path.glob("ledger.corrupt.*.json"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == payload


@pytest.mark.parametrize(
    "mutation",
    [
        {"in_flight_op": {}},
        {
            "in_flight_op": {
                "op": "upload",
                "slot": "main",
                "pre_ids": None,
                "request_id": None,
            }
        },
        {"recent_requests": {"request": {"status": "in_progress"}}},
        {"active_sequence": {"generation": "invalid"}},
    ],
)
def test_invalid_nested_ledger_records_fail_closed(tmp_path, tv, mutation):
    payload = Ledger(duid="uuid:test").model_dump()
    payload.update(mutation)
    atomic_json(tmp_path / "ledger.json", payload)
    restored = load_ledger(tmp_path, "uuid:test")
    assert restored.owned_ids == []
    assert len(list(tmp_path.glob("ledger.corrupt.*.json"))) == 1
    Engine(tmp_path, restored, tv).execute(
        "clear", {"slots": None, "restore_baseline": True}
    )
    assert not any(c[0] == "delete" for c in tv.calls)
    assert tv.images == {"USER_BASE", "USER_PHOTO"}


def test_orphan_revision_install_cannot_reuse_its_revision(tmp_path, tv):
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / "main__r8.jpg").write_bytes(b"orphaned-install")
    atomic_json(tmp_path / "ledger.json", Ledger(duid="uuid:test").model_dump())
    restored = load_ledger(tmp_path, "uuid:test")
    assert restored.next_revision == 9 and restored.owned_ids == []
    result = put(Engine(tmp_path, restored, tv))
    assert result["revision"] == 9
    assert Path(result["cached_image_path"]).name == "main__r9.jpg"
    assert result["content_id"] in restored.owned_ids


def test_human_override_between_gc_deletes_is_not_deleted(engine, tv):
    put(engine)
    put(engine, "b", show=False)
    chosen = put(engine, "c", show=False)["content_id"]
    generation = engine.state.control_generation
    tv.before_delete = lambda device: setattr(device, "current_id", chosen)
    result = engine.execute("clear", {"slots": ["b", "c"], "restore_baseline": True})
    assert engine.state.user_override_detected
    assert (
        engine.state.control_generation == generation + 2
    )  # Clear, then viewer override.
    assert chosen not in result["deleted_content_ids"]
    assert chosen in engine.state.pending_delete_ids and chosen in tv.images
    assert tv.current_id == chosen


def test_eviction_fresh_guard_preserves_viewer_change(engine, tv):
    put(engine)
    prior_selections = [c for c in tv.calls if c[0] == "select"]

    def change_before_eviction(device):
        if engine.state.pending_baseline_restore:
            device.current_id = "USER_PHOTO"

    tv.before_probe = change_before_eviction
    result = engine.execute("clear", {"slots": None, "restore_baseline": True})
    assert result["user_override_detected"]
    assert tv.current_id == "USER_PHOTO"
    assert [c for c in tv.calls if c[0] == "select"] == prior_selections
    assert {"USER_BASE", "USER_PHOTO"} <= tv.images


def test_ttl_starts_at_confirmed_display_time(engine, tv):
    original_select = tv.select

    def select(cid):
        original_select(cid)
        engine.clock = lambda: 1005.0

    tv.select = select
    result = put(engine, ttl=10)
    assert result["displayed"]["expires_at"] == 1015.0
    assert result["slots"]["main"]["expires_at"] == 1015.0


def test_stale_revision_timer_cannot_expire_new_revision(engine, tv):
    first = put(engine, ttl=10)
    second = put(engine, ttl=20)
    engine.clock = lambda: 1021.0
    result = engine.execute("expire", {"slot": "main", "revision": first["revision"]})
    assert result["slots"]["main"]["revision"] == second["revision"]
    assert tv.current_id == second["content_id"]
    engine.execute("expire", {"slot": "main", "revision": second["revision"]})
    assert tv.current_id == "USER_BASE" and not engine.state.slots


def test_atomic_commit_syncs_file_then_replace_then_directory(tmp_path, monkeypatch):
    calls = []
    original_sync, original_replace = os.fsync, os.replace

    def sync(fd):
        calls.append("sync")
        original_sync(fd)

    def replace(a, b):
        assert Path(a).name == f"ledger.json.tmp.{os.getpid()}"
        calls.append("replace")
        original_replace(a, b)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", replace)
    atomic_json(tmp_path / "ledger.json", {"owned_ids": []})
    assert calls == ["sync", "replace", "sync"]
    assert json.loads((tmp_path / "ledger.json").read_text()) == {"owned_ids": []}
    assert not list(tmp_path.glob("*.tmp.*"))


async def test_cancelled_worker_retains_flock_and_shutdown_awaits_completion(
    tmp_path, tv
):
    entered, release = threading.Event(), threading.Event()
    original = tv.probe

    def blocking_probe(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    tv.probe = blocking_probe
    worker = DeviceWorker(tmp_path, "uuid:test", lambda: tv)
    task = asyncio.create_task(worker.run("inspect", {}))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with (tmp_path / "ledger.lock").open("a+b") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        close = asyncio.create_task(worker.close())
        await asyncio.sleep(0)
        assert not close.done()
        release.set()
        await close
        with (tmp_path / "ledger.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        release.set()
        await worker.close()
