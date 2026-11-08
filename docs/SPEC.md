# `frame-art-mcp` Complete Specification (v2 — Post-Review Revision)

## 1. Purpose, Scope, and Repository Boundary

`frame-art-mcp` is a standalone, zero-preset Model Context Protocol (MCP) server that turns a Samsung The Frame TV (verified on 65" `QA65LS03BAKXXS`, Matte Display panel, Art API `v4.3.4.0`) into a safe, optically conditioned 4K visual output surface for AI Agents.

It supports two unified scenarios through the exact same **4 mechanism tools** (`frame_inspect`, `frame_put`, `frame_switch`, `frame_clear`):
1. **Trompe-l'œil Ambient Art & Diegetic Signals**: Full-bleed `3840x2160` artworks, 1:1 macro viewports cropped from gigapixel masterpieces, time-sliced sequences, dynamic material re-lighting/re-pigmenting, and subtle <4% physical marginalia (deckle-edge cotton Cartellinos, cinnabar seals, intaglio plate legends).
2. **General-Purpose 65-Inch Agent Display ("大屏看图")**: Any Agent temporarily projecting an architecture diagram, plot, UI screenshot, rasterized document page, photo, or A/B visual comparison of any aspect ratio onto the 65" screen without aspect-ratio distortion, transparent-PNG black-out, or album pollution.

### Repository & Deployment Boundary
- **Standalone Repository**: `/Users/tefx/Projects/frame-art-mcp` (remote `https://github.com/Tefx/frame-art-mcp`, semantic tag `v0.1.0`).
- **Why Separate from `ha-samsungtv-smart`**: `/Users/tefx/Projects/HACSWorkspace/ha-samsungtv-smart` is a Home Assistant custom component fork running inside the HA container. Keeping `frame-art-mcp` separate avoids adding heavy 4K image processing (`numpy`, `scipy`, `Pillow`) to HA and keeps upstream merges clean.
- **Self-Contained Guidance (Zero Global `SKILL.md` Pollution)**: No global Skill is added to `dotfiles/agent/skills/`. Instead:
  1. The core Matte Display optical rules and mode selection guidance (`matte_natural` for art/ambient vs `raw` for color-exact screenshots/charts) are embedded directly in `frame_put`'s tool docstring.
  2. Full copy-pasteable 15-line NumPy/PIL material recipes are accessible both via `frame_inspect(include_optics_guide=True)` (works across all tool-only bridges like Tela) and via the MCP Resource `frame://guide/matte-optics`.
- **Tela Registration (`/Users/tefx/dotfiles/agent/tela.yaml`)**:
  Registered under `servers.frame` (`tool_prefix: "frame_"`, `default_posture: "read_write"`, `frame_inspect: "read_only"`), inheriting `tela`'s `"exposure": "deferred"` in `/Users/tefx/dotfiles/agent/mcp/pi.mcp.json`.

---

## 2. Zero-Config Auto-Discovery, Target Binding & Storage Layout

### 2.1 3-Tier Discovery & Strict DUID Pinning (`discovery.py`)
Samsung The Frame TVs expose an unauthenticated REST metadata endpoint at `GET http://<ip>:8001/api/v2/` returning `device.FrameTVSupport == "true"`, `device.duid` (`uuid:...`), `device.PowerState` (`"on"` or `"standby"`), and `device.resolution` (`"3840x2160"`).

**Target Resolution & Anti-Hijack Invariant**:
1. **Global `FRAME_TV_DUID` Constraint**: If `FRAME_TV_DUID` env var is set, it constrains **all** discovery tiers, first-run binding, and every write call. If `devices.json` has a `bound_duid != FRAME_TV_DUID`, return `status="duid_mismatch"`. If `FRAME_TV_DUID` is not reachable on the LAN, return `status="target_unavailable"`—never bind or write to a substitute TV.
2. **Explicit Host Override**: If `host` argument or `FRAME_TV_HOST` env var is provided, probe `http://<host>:8001/api/v2/` (timeout `1.5s`), verify `device.FrameTVSupport == "true"`, and read its authoritative `duid`. (If `bound_duid` or `FRAME_TV_DUID` is set and does not match `device.duid`, abort with `status="duid_mismatch"`). A one-off `host` argument in `frame_inspect` does not overwrite `bound_duid` in `devices.json`.
3. **Bound DUID Fast Path**: Once `bound_duid` is established (via `FRAME_TV_DUID` or `devices.json`):
   - **Tier 1 (Cached IP)**: Probe the cached IP for `bound_duid` (`timeout=0.6s`). If reachable and `device.duid == bound_duid`, use it.
   - **Tier 2 (SSDP Relocation)**: If unreachable (e.g., DHCP IP changed), send UDP `M-SEARCH` (`239.255.255.250:1900`, `ST: urn:samsung.com:device:RemoteControlReceiver:1`, wait `1.0s`) and check responses for `device.duid == bound_duid`.
   - **Tier 3 (Subnet Relocation)**: Probe active private IPv4 `/24` subnet on `:8001/api/v2/` (`asyncio.Semaphore(48)`, connect timeout `0.4s`, total budget `~2.5s`) looking **only** for `device.duid == bound_duid`.
   - **Strict Pinning Rule**: If `bound_duid` cannot be found, return `status="target_unavailable"`—**never** silently rebind to a different Frame TV on the LAN!
4. **First-Run Auto-Binding**: If neither `bound_duid` nor `FRAME_TV_DUID` exists yet, run Tier 2 $\rightarrow$ Tier 3 for any device with `FrameTVSupport == "true"`. If exactly one is found, bind its `duid` in `devices.json`. If multiple are found, return `status="ambiguous_multiple_tvs"` with the candidate list.

### 2.2 Persistent State Directory (`~/.local/share/frame-art-mcp/`)
```text
~/.local/share/frame-art-mcp/
├── devices.json                    # { "bound_duid": "uuid:...", "devices": { "<duid>": {ip, name, model, last_seen} } }
└── <sanitized_duid>/
    ├── token.txt                   # Port 8002 SSL WebSocket pairing token
    ├── ledger.lock                 # fcntl.flock file lock (acquired by serialized worker thread)
    ├── ledger.json                 # Crash-safe state ledger (temp write + fsync + os.replace + dir fsync)
    ├── blobs/
    │   └── <slot>__r<rev>.jpg      # Immutable per-revision 3840x2160 JPEG cache (never overwritten in place)
    └── previews/                   # Bounded LRU cache (max 16 entries, auto-pruned by mtime)
        ├── <sha12>_full.jpg
        ├── <sha12>_detail_512.png
        └── <sha12>_tonal_512.png
```

---

## 3. Hardware Safety Gate, Concurrency & Crash-Safe Ledger (`ledger.py`, `tv_client.py`)

### 3.1 Authoritative 4-State Mode Probe & Hard Safety Gate
Because the TV's Art WebSocket (`:8002`) can return a cached `artmode == "on"` while the panel is in standby, every mode probe returns one of **4 states** (`"art" | "tv" | "off" | "unknown"`):
1. `GET http://<ip>:8001/api/v2/` (timeout `1.5s`):
   - If `device.PowerState == "standby"` $\rightarrow$ `tv_mode = "off"`, `safe_to_project = False`.
   - If HTTP request times out, fails, or returns an unrecognized `PowerState` $\rightarrow$ `tv_mode = "unknown"`, `safe_to_project = False`.
2. If and only if `device.PowerState == "on"`:
   - **Non-Interactive `frame_inspect` Guard**: If called from `frame_inspect` (read-only), `frame_inspect` NEVER initiates interactive pairing or falls back to unauthenticated/new-token handshakes: if `device.TokenAuthSupport == "true"` and `token.txt` is missing, empty, or rejected by `:8002`, return `tv_mode = "unknown"`, `safe_to_project = False`, `reason = "pairing_required"` immediately.
   - Otherwise, query `:8002` `art.get_artmode()` (timeout `3.0s`):
     - If `"on"` $\rightarrow$ `tv_mode = "art"`, `safe_to_project = True`.
     - If `"off"` $\rightarrow$ `tv_mode = "tv"`, `safe_to_project = False`.
     - If WebSocket handshake/command fails or times out, or if TV requires interactive pairing approval $\rightarrow$ `tv_mode = "unknown"`, `safe_to_project = False` (with `reason="ws_error"` or `"pairing_required"`).

**Absolute Safety Invariants**:
1. `frame-art-mcp` NEVER sends remote key presses, power-on commands, or `set_artmode("on")`.
2. **All** device writes (`upload`, `select_image`, `delete`) require `tv_mode == "art"`. Whenever `tv_mode in ("tv", "off", "unknown")`, the safety gate is closed.
3. `frame_inspect` is **100% read-only on the TV**: it NEVER calls `upload`, `select_image`, or `delete`.

### 3.2 Crash-Safe WAL Ledger Schema (`ledger.json`)
To prevent corrupting state or deleting user photos, `ledger.json` separates **confirmed MCP-owned IDs (`owned_ids`)**, **currently displayed state (`displayed`)**, **staged slot revisions (`slots`)**, and **unattributed crash candidates (`unattributed_candidates`)**:

```json
{
  "schema_version": 3,
  "duid": "uuid:c142370e-b312-44d5-9b35-090b08162efd",
  "control_generation": 12,
  "next_revision": 4,
  "baseline_content_id": "SAM-S1110760",
  "expected_displayed_id": "MY_F0205",
  "displayed": {
    "slot": "main",
    "revision": 3,
    "content_id": "MY_F0205",
    "cached_image_path": "/Users/tefx/.local/share/frame-art-mcp/.../blobs/main__r3.jpg",
    "expires_at": null
  },
  "display_history": [
    {"slot": "main", "revision": 3, "content_id": "MY_F0205"}
  ],
  "user_override_detected": false,
  "pending_baseline_restore": false,
  "active_sequence": null,
  "slots": {
    "main": {
      "revision": 3,
      "content_id": "MY_F0205",
      "cached_image_path": "/Users/tefx/.local/share/frame-art-mcp/.../blobs/main__r3.jpg",
      "summary": "Lunar tidal sand ripples around basalt pebble",
      "metadata": {"tide_phase": 0.35},
      "owner": "forgemaster",
      "created_at": "2026-11-08T18:18:00Z",
      "expires_at": null,
      "optical_diagnostics": { ... }
    }
  },
  "owned_ids": ["MY_F0205"],
  "pending_delete_ids": [],
  "unattributed_candidates": [],
  "in_flight_op": null,
  "recent_requests": {}
}
```
- **Crash-Safe Commit Protocol**: Every update to `ledger.json` writes to `ledger.json.tmp.<pid>`, calls `os.fsync(fd)`, executes `os.replace(tmp, "ledger.json")`, and calls `os.fsync(dir_fd)`. If `ledger.json` is ever corrupted on disk, move it to `ledger.corrupt.<ts>.json` and initialize a clean ledger with `owned_ids=[]`—**never** auto-delete any remote `MY_Fxxxx` when ledger integrity is lost.

### 3.3 Ownership, Human-Override & Reconciliation Rules
1. **Strict Ownership Rule (Zero Risk of Deleting User Photos)**:
   - A `content_id` enters `owned_ids` **only** when `art.upload()` returns that exact `content_id` to `frame-art-mcp`.
   - If a previous `in_flight_op` of type `"upload"` timed out or crashed before returning a `content_id`, the diff `current_my_photos - set(in_flight_op.pre_ids) - set(owned_ids)` is appended to `unattributed_candidates` (surfaced in `frame_inspect` for manual review) and is **NEVER added to `owned_ids` or `pending_delete_ids`**.
   - Every `art.delete(del_id)` call MUST assert `del_id in owned_ids` and `del_id != baseline_content_id`.
2. **Human Override & Interrupted-Select Recovery (`expected_displayed_id`)**:
   Whenever `tv_mode == "art"` and `current_id = art.get_current()["content_id"]` is observed:
   - If `in_flight_op` of type `"select"` (`{slot, revision, target_id, cached_image_path, pre_select_id, expires_at}`) was interrupted by a timeout/crash:
     - If `current_id == target_id`:
       - If `slot is None` (this was a baseline restore): commit `displayed = None`, `expected_displayed_id = None`, `pending_baseline_restore = False`, `baseline_content_id = target_id`, and clear `in_flight_op`.
       - Else (managed slot revision): commit `displayed = {"slot": slot, "revision": revision, "content_id": target_id, "cached_image_path": cached_image_path, "expires_at": expires_at}`, `expected_displayed_id = target_id`, `user_override_detected = False`, `pending_baseline_restore = False`, push `{"slot": slot, "revision": revision, "content_id": target_id}` to `display_history`, update `slots[slot].expires_at = expires_at` only if `slot in slots and slots[slot].revision == revision`, re-arm in-process TTL timer if `expires_at` is set, and clear `in_flight_op`.
     - Else if `current_id == pre_select_id`: the select did not take effect; clear `in_flight_op` while keeping prior `displayed` and `expected_displayed_id = pre_select_id`.
     - Else (`current_id` is a third image): clear `in_flight_op` and fall through to the human-override branch below.
   - If `expected_displayed_id is not None` and `current_id != expected_displayed_id`:
     - A human (or external remote/app) changed the displayed artwork! (This catches both switching to an Art Store / personal photo AND switching to another `MY_Fxxxx` via the TV UI).
     - Set `user_override_detected = True`, `expected_displayed_id = None`, `displayed = None`, `pending_baseline_restore = False`, `active_sequence = null`, and increment `control_generation += 1` (invalidating all background sequence/restore tasks).
     - If `current_id not in owned_ids`, update `baseline_content_id = current_id`.
   - If `expected_displayed_id is None` and `current_id not in owned_ids`:
     - Update `baseline_content_id = current_id`.
3. **Write-Path Reconciliation (Executed Only by Write Tools / Authorized Background Tasks when `tv_mode == "art"`)**:
   - **Step A (Observation & Override Check)**: Run Rule 1 and Rule 2 above.
   - **Step B (TTL Expiry Sweep for Both Staged Slots & Active Displayed Revision)**:
     - For each `slot_name, slot_entry` in `list(slots.items())` where `slot_entry.expires_at is not None and slot_entry.expires_at <= now`:
       - Remove `slots[slot_name]`, remove matching `(slot_name, slot_entry.revision)` entries from `display_history`, and add `slot_entry.content_id` to `pending_delete_ids`.
       - If `displayed` is pointing to `(slot_name, slot_entry.revision)`, set `pending_baseline_restore = True`.
     - Additionally, if `displayed is not None` and `displayed.expires_at is not None and displayed.expires_at <= now` (which covers the case where `displayed` is an older revision `r1` with a TTL that was superseded in `slots[slot]` by a `show=False` staging of `r2`):
       - Add `displayed.content_id` to `pending_delete_ids`, remove `(displayed.slot, displayed.revision)` from `display_history`, and set `pending_baseline_restore = True`.
   - **Step C (Screen Eviction Before Delete, with Fresh Mode/Override Re-Check & WAL)**:
     If `pending_baseline_restore` is `True` and `not user_override_detected`:
     - Re-verify `tv_mode == "art"` and re-read `current_id = art.get_current()["content_id"]`. If `expected_displayed_id is not None and current_id != expected_displayed_id`, trigger Rule 2 (human override) and abort eviction.
     - If `current_id in pending_delete_ids`:
       - Filter `display_history` for eligible entries `(slot, rev, cid)` where `slot in slots`, `slots[slot].revision == rev`, `(slots[slot].expires_at is None or slots[slot].expires_at > now)`, and `cid not in pending_delete_ids`.
       - Select `fallback` as the most recent eligible entry in the filtered list; if the filtered list is empty (regardless of whether raw `display_history` was empty), use `baseline_content_id` (`slot=None, rev=None`).
       - If `fallback_id` exists: persist `in_flight_op = {"op": "select", "slot": fb_slot, "revision": fb_rev, "target_id": fallback_id, "cached_image_path": fb_path, "pre_select_id": current_id, "expires_at": fb_exp}`, call `art.select_image(fallback_id, show=True)`, verify `art.get_current()["content_id"] == fallback_id`, update `current_id = fallback_id`, `expected_displayed_id = (fallback_id if fb_slot is not None else None)`, `displayed = ({slot: fb_slot, revision: fb_rev, content_id: fallback_id, cached_image_path: fb_path, expires_at: fb_exp} if fb_slot is not None else None)`, `pending_baseline_restore = False`, and clear `in_flight_op`.
       - If `fallback_id` is missing (no eligible fallback before `select_image` is called), keep `current_id` on screen and report `cleanup_pending`. If `select_image` or its verification times out / disconnects, leave `in_flight_op` intact for Rule 2 reconciliation, abort Step D deletion, and return `status="outcome_unknown"`.
   - **Step D (Confirmed Garbage Collection with Per-Delete Fresh Guard)**:
     For each `del_id` in `list(pending_delete_ids)`:
     - Re-verify `tv_mode == "art"` and re-read `current_id = art.get_current()["content_id"]`. If no longer `"art"`, stop GC. If `expected_displayed_id is not None and current_id != expected_displayed_id`, trigger Rule 2 (human override) and recompute `referenced_ids`.
     - Compute fresh referenced IDs:
       $$\text{referenced\_ids} = \{baseline\_content\_id, current\_id, expected\_displayed\_id\} \cup \{s.content\_id \mid s \in slots.values()\} \cup (\{displayed.content\_id\} \text{ if } displayed \text{ else } \emptyset)$$
     - If `del_id in owned_ids` and `del_id not in referenced_ids`:
       - Call `res = art.delete(del_id)`. Remove `del_id` from `pending_delete_ids` and `owned_ids` (and delete its unreferenced `blobs/<slot>__r<rev>.jpg`) **only** if `res` is truthy or `del_id` is confirmed absent from `art.available("MY-C0002")`.

### 3.4 Serialized Worker, Upload-With-Staging & Race-Window Reduction (`frame_put`)
Because `samsungtvws` uses synchronous SSL sockets and a single WebSocket connection cannot safely multiplex concurrent readers, all TV I/O for a device runs through a single serialized worker thread.
- **Cancellation-Safe Lock Ownership**: `flock(ledger.lock)` is acquired and released **inside the synchronous worker function** (never in an outer async coroutine that could release `flock` upon `asyncio.CancelledError` while the synchronous socket upload/select is still executing!). On MCP server shutdown / `stdio` EOF, the server awaits completion of any in-flight worker transaction before closing.
- **Timeouts**: `:8001` REST timeout `= 1.5s`; `:8002` WS command timeout `= 4.0s`; D2D image upload timeout `= 15.0s`.
- **`frame_put` Transaction (`dry_run=False`)**:
  1. Run optical pipeline locally (outside device lock) and write normalized JPEG to a unique temporary path `blobs/tmp_<pid>_<uuid4>.jpg` with `os.fsync`.
  2. Submit transaction to the serialized worker and acquire `flock(ledger.lock)`.
     - **Idempotency Protocol Across All Write Tools (`frame_put`, `frame_switch`, `frame_clear`)**:
       - If `request_id` is provided and exists in `ledger.recent_requests`:
         - If `params_hash` differs from the recorded request, delete temp file (if any), release lock, and return `status="request_id_conflict"`.
         - If recorded status is `"in_progress"` or `"outcome_unknown"`, delete temp file (if any), run full reconciliation (Steps A–D) first, update `recent_requests[request_id]` with the reconciled outcome, and return it without re-executing the side effect.
         - Otherwise, delete temp file (if any) and return the cached terminal response immediately.
       - Before the first device side effect, persist `recent_requests[request_id] = {"params_hash": params_hash, "status": "in_progress", "updated_at": now}` in `ledger.json` and update it atomically at each state commit.
  3. Check Gate #1 (`tv_mode == "art"`). If not `"art"`, delete temp file, release lock, and return `status="blocked_by_tv_mode", tv_mode=tv_mode, projected=False, staged=False`.
  4. Run full Write-Path Reconciliation (Steps A, B, C, D). Record `pre_upload_current_id = current_id` and `pre_ids = [x["content_id"] for x in art.available("MY-C0002")]`. Persist `in_flight_op = {"op": "upload", "slot": slot, "pre_ids": pre_ids, "request_id": request_id}`.
  5. Upload JPEG bytes (`matte="none", portrait_matte="none"`).
     - If upload raises a timeout/network error: delete temp file, leave `in_flight_op` for reconciliation, record `status="outcome_unknown"` for `request_id`, release lock, and return `status="outcome_unknown"`.
     - On success (`new_id`): allocate monotonic global revision `new_rev = ledger.next_revision` (`ledger.next_revision += 1`), atomically install `os.replace(tmp_path, f"blobs/{slot}__r{new_rev}.jpg")` + `fsync(blobs_dir)`, and in a **single atomic `ledger.json` commit**:
       - Add `new_id` to `owned_ids`;
       - Move any previous `old_id` of `slots[slot]` (`old_id != new_id`) into `pending_delete_ids` (if `show=False` and `old_id` is still on screen, `displayed` still references `old_id` and its `displayed.expires_at`, so Step D keeps `old_id` alive until replaced on screen or expired);
       - Set `slots[slot]` to `{revision: new_rev, content_id: new_id, cached_image_path: ..., expires_at: (now + ttl_sec) if ttl_sec else None, ...}`;
       - Clear `in_flight_op = null`.
       - Schedule/refresh the in-process expiry task for `(slot, new_rev)`.
  6. If `show=True`:
     - Increment `control_generation += 1` and clear `active_sequence = null` (note: advancing `control_generation` cancels active sequences, while TTL timers remain bound to `(slot, revision)` and `displayed.revision` so unexpired TTLs on any slot continue to be enforced).
     - **Gate #2 (Race-Window Reduction before Select)**: Re-verify `tv_mode == "art"` AND verify `art.get_current()["content_id"] == pre_upload_current_id`.
     - If TV left `"art"` mode during upload: skip `select_image`, commit ledger, and return `status="staged_only_tv_left_art_mode", projected=False, staged=True`.
     - If `current_id != pre_upload_current_id` (human changed artwork during the 3s upload): set `user_override_detected = True`, skip `select_image`, commit ledger, and return `status="staged_only_user_override_during_upload", projected=False, staged=True`.
     - Otherwise, persist `in_flight_op = {"op": "select", "slot": slot, "revision": new_rev, "target_id": new_id, "cached_image_path": ..., "pre_select_id": pre_upload_current_id, "expires_at": (now + ttl_sec) if ttl_sec else None}`, call `art.select_image(new_id, show=True)`, verify `art.get_current()["content_id"] == new_id`, compute `exp = (confirmed_display_time + ttl_sec) if ttl_sec else None`, commit `displayed = {slot, new_rev, new_id, path, expires_at: exp}`, `slots[slot].expires_at = exp`, `expected_displayed_id = new_id`, `user_override_detected = False`, push `{slot, new_rev, new_id}` to `display_history`, and clear `in_flight_op`.
  7. Run Step C & Step D garbage collection (so any superseded `old_id` is deleted only **after** `new_id` is confirmed on screen), record idempotent result in `recent_requests[request_id]`, commit `ledger.json`, and release lock.

### 3.5 Process Lifetime Contract for TTL & Sequences
- **In-Session Active Timers + Lazy Write-Path Convergence**:
  - `frame-art-mcp` runs as a `stdio` MCP server without requiring a separate background daemon.
  - While the `stdio` server process is alive, active `ttl_sec` timers (bound to `(slot, revision)` for slot cleanup, and checking `displayed == (slot, revision)` + `!user_override_detected` before screen eviction) and `frame_switch` sequences execute automatically in the background asyncio loop.
  - If the MCP process terminates before `expires_at` (or while the TV is `"tv"`/`"off"`), `expires_at` remains persisted in `ledger.json`. Read-only `frame_inspect` reports expired slots without mutating the TV, while the next write tool call (`frame_put`, `frame_switch`, `frame_clear`) when `tv_mode == "art"` executes Step B–D reconciliation to evict and garbage-collect expired slots.
  - Multi-step `frame_switch` sequences are strictly bound to `control_generation`: on every step (and before final `restore_on_done`), the worker re-acquires the lock and verifies `ledger.control_generation == my_generation`, `tv_mode == "art"`, and `art.get_current()["content_id"] == expected_displayed_id`. Whenever the MCP observes that the TV left `"art"` mode or a human overrode the image, or when the MCP process exits, the sequence terminates immediately and never auto-resumes. (`frame_switch` also rejects any target slot whose `expires_at <= now` with `status="slot_expired"`.)

---

## 4. Optical Conditioning, Aspect-Ratio Framing & Diagnostics (`optics.py`)

To prevent mat borders from distorting optical diagnostics or hijacking retinal crops, and to support transparent diagrams/screenshots cleanly, `optics.py` executes in a **strict 5-stage order**:

### Stage 1: Input Decoding, Orientation, Color Profile & Alpha Pre-Compositing
1. Guard maximum input pixel count (`<= 120,000,000` pixels; reject multi-page/animated files or require a single rasterized page).
2. Apply `ImageOps.exif_transpose(img)` so camera photos respect EXIF orientation.
3. Convert embedded ICC profiles (e.g., macOS Display P3, Adobe RGB) or CMYK/16-bit channels to standard float32 sRGB `[0.0 .. 255.0]`.
4. **Transparent PNG (`RGBA` / `LA`) Pre-Compositing**:
   If the image has an alpha channel with $\min(\alpha) < 255$:
   - Compute median luminance $L_{\text{fg}} \in [0, 1]$ of foreground pixels ($\alpha > 32/255$).
   - Choose backing color: if `fit == "pad_slate"` or (`fit == "contain"` and $L_{\text{fg}} \ge 0.55$, i.e. light text/lines on transparent dark-mode background), composite over matte mineral slate `#222428` (`34, 36, 40`); otherwise (dark lines/text on transparent background, typical of Mermaid/Graphviz/Matplotlib) composite over warm gallery paper `#EAE4D7` (`234, 228, 215`).

### Stage 2: Exact Aspect-Ratio Content Scaling (`fit`)
Target output frame is `3840 x 2160` (`16:9`).
- `fit="contain"` *(default)*, `"pad_paper"`, `"pad_slate"`:
  - **Zero Aspect-Ratio Distortion**: If and only if `W * 9 == H * 16`, resize directly to `3840x2160` via `Image.Resampling.LANCZOS` (`content_bbox = [0, 0, 3840, 2160]`).
  - For **all** other aspect ratios, scale uniformly via Lanczos so the image fits inside `3840x2160` (`W_c = max(1, round(W * scale))`, `H_c = max(1, round(H * scale))` where `scale = min(3840 / W, 2160 / H)`, preserving exact aspect ratio) centered at `content_bbox = [x0, y0, x0 + W_c, y0 + H_c]`.
- `fit="cover"`:
  - Center-crop input to exact `16:9` and resize via Lanczos to `3840x2160` (`content_bbox = [0, 0, 3840, 2160]`).

### Stage 3: Content-Scoped Pre-LUT Diagnostics & `optical_grade` Tonal Mapping
All optical metrics are evaluated **exclusively on the scaled content region `W_c x H_c` (`content_bbox`)** so letterbox/pillarbox mat margins never skew percentages:
- **`optical_grade="matte_natural"` *(default for art & ambient scenes)*:**
  Applies heuristic tonal conditioning in float32 sRGB `[0.0 .. 255.0]` to reduce LCD backlight gray-black glow and harsh peak-white glare (note: actual physical luminance depends on the TV's ambient light sensor and brightness setting):
  1. **$C^1$-Continuous Hermite Shadow Toe-Lift (`c < 64.0`, floor $\mathbf{c}_{\min} = (22.0, 22.0, 21.0)$)**:
     For each channel $c \in \{R, G, B\}$:
     $$t_c = \text{clip}\left(\frac{c}{64.0},\, 0.0,\, 1.0\right), \quad c_{\text{toe}} = c_{\min, c}\cdot (1.0 - t_c)^2 + c$$
     Satisfies $f(0) = c_{\min, c}$, $f(64) = 64$, and $f'(64) = 1.0$ with zero step discontinuity and zero division-by-zero chroma noise.
  2. **$C^1$-Continuous Highlight Roll-Off (`c > 215.0`, peak paper white $\mathbf{c}_{\max} = (243.0, 241.0, 236.0)$)**:
     $$u_c = \text{clip}\left(\frac{c_{\text{toe}} - 215.0}{255.0 - 215.0},\, 0.0,\, 1.0\right), \quad c_{\text{hi}} = c_{\text{toe}} - (255.0 - c_{\max, c})\cdot u_c^2$$
     Satisfies $f(215) = 215$, $f'(215) = 1.0$, and $f(255) = c_{\max, c}$.
  3. **Reflective Pigment Gamut Soft-Compression**:
     For pixels with HSV saturation $S > 0.88$, smoothly compresses $S \mapsto 0.88 + 0.06 \cdot \tanh((S - 0.88)/0.06)$.
- **`optical_grade="raw"` *(recommended for color-exact UI screenshots, charts & photos)*:**
  Leaves the scaled `W_c x H_c` content pixels completely untouched.

### Stage 4: Gallery Mat Placement (When $W_c \times H_c \ne 3840 \times 2160$) & Deterministic Dithering
1. If $W_c \times H_c \ne 3840 \times 2160$:
   - Select mat color: `#EAE4D7` (`234, 228, 215`) for `"pad_paper"` or when `"contain"` outer-8px content border median luminance $L_{\text{edge}} \ge 0.42$; `#222428` (`34, 36, 40`) for `"pad_slate"` or when $L_{\text{edge}} < 0.42$.
   - Place the `W_c x H_c` content into the center of the `3840x2160` mat canvas **without** drawing any artificial bevel or drop-shadow line.
2. **Deterministic Dithering & `4:4:4` Encoding**:
   - Seed a local `np.random.Generator(np.random.PCG64(seed))` using the first 8 bytes of `sha256(input_file_bytes)` so `dry_run=True` and `dry_run=False` produce identical output bytes.
   - When `optical_grade == "matte_natural"` (or on the mat margin), add zero-mean triangular dither ($\pm 1.0\text{ LSB}$) before rounding and clipping to `uint8`.
   - Encode final `3840x2160` image as JPEG with `quality=96, subsampling=0` (`4:4:4`).

### Stage 5: Content-Scoped Diagnostics & 1:1 Retinal Crops
Computed on `content_bbox` (`W_c x H_c`) in float32 `L = 0.299*R + 0.587*G + 0.114*B` (`[0..255]`):
- `max_dark_blob_pct`: Partition `content_bbox` into $\lceil H_c / 8 \rceil \times \lceil W_c / 8 \rceil$ cells (computing mean luminance per cell, including partial edge cells); label connected components of `cell_mean_L < 22.0` using 8-connectivity (`scipy.ndimage.label` with `np.ones((3,3))`). Return `largest_component_cell_count / total_cells * 100.0` (`0.0` if no cell $< 22.0$). Warns if `post_lut > 3.0%`.
- `synthetic_flatness_pct`: Evaluated **before dithering** on `content_bbox`: if `min(W_c, H_c) < 3`, return `0.0`; otherwise partition `content_bbox` into $\lceil H_c / 16 \rceil \times \lceil W_c / 16 \rceil$ blocks and compute the percentage of blocks where the mean absolute $3\times3$ discrete Laplacian $\mu_{|\nabla^2 L|} < 0.75\text{ LSB}$ (catches both flat fills and linear mathematical gradients). Informational for diagrams; warns for realism art if `> 25.0%`.
- `neon_gamut_pct`: Percentage of `content_bbox` pixels with $S > 0.90$ and $V > 0.80$.
- `glare_pct`: Percentage of `content_bbox` pixels with $L > 245.0$.
- `transition_delta_pct`: Mean absolute luminance difference (`0..100%`) between the new `3840x2160` frame and `displayed.cached_image_path` (if any).
- `retinal_crops` (extracted from the **post-LUT final image** inside `content_bbox`):
  - If `min(W_c, H_c) < 64`: return `detail_crop_path = null`, `tonal_crop_path = null`, `crop_skipped_reason = "content_dimension_below_64px"` (never fail the projection or upscale).
  - Otherwise, let `inset_x = 16 if W_c >= 544 else 0`, `inset_y = 16 if H_c >= 544 else 0`, `crop_w = min(512, W_c - 2*inset_x)`, `crop_h = min(512, H_c - 2*inset_y)` (always 1:1 native pixels, never upscaled):
    - `detail_crop_path`: `crop_w x crop_h` lossless PNG crop centered on the window inside inset `content_bbox` with maximum Sobel gradient energy.
    - `tonal_crop_path`: `crop_w x crop_h` lossless PNG crop centered on the window inside inset `content_bbox` with minimum mean luminance.

---

## 5. Exact MCP Tool & Resource Contracts (`server.py`)

### Tool 1: `frame_inspect` (`read_only`)
Read-only inspection of the TV's current mode, safety gate status, baseline artwork, managed slots, and optional Matte Display optical guide. Performs **zero** device writes.
- **Arguments**:
  - `include_optics_guide: bool = False` — If `True`, includes the full Matte Display optical rules and copy-pasteable 15-line Python/NumPy/PIL material recipes in `optics_guide`.
  - `host: str | None = None` — Optional IP override for this inspection call.
- **Returns**:
  ```json
  {
    "tv_reachable": true,
    "tv_info": {
      "name": "Samsung The Frame 65",
      "model": "QA65LS03BAKXXS",
      "duid": "uuid:c142370e-b312-44d5-9b35-090b08162efd",
      "ip": "192.168.88.6",
      "resolution": [3840, 2160]
    },
    "tv_mode": "art",
    "safe_to_project": true,
    "control_generation": 12,
    "observed_current_id": "MY_F0205",
    "expected_displayed_id": "MY_F0205",
    "displayed": {
      "slot": "main",
      "revision": 3,
      "content_id": "MY_F0205",
      "cached_image_path": "/Users/tefx/.local/share/frame-art-mcp/.../blobs/main__r3.jpg"
    },
    "baseline_content_id": "SAM-S1110760",
    "user_override_detected": false,
    "pending_baseline_restore": false,
    "active_sequence": null,
    "slots": {
      "main": {
        "revision": 3,
        "content_id": "MY_F0205",
        "cached_image_path": "/Users/tefx/.local/share/frame-art-mcp/.../blobs/main__r3.jpg",
        "summary": "Lunar tidal sand ripples",
        "metadata": {},
        "owner": "forgemaster",
        "created_at": "2026-11-08T18:18:00Z",
        "expires_at": null,
        "optical_diagnostics": { ... }
      }
    },
    "owned_ids_count": 1,
    "pending_delete_ids": [],
    "unattributed_candidates": [],
    "optics_guide": null
  }
  ```

### Tool 2: `frame_put` (`read_write`)
**Docstring embedded in tool metadata**:
> Project or stage any local raster image (`JPG`, `PNG`, `WebP`, `TIFF`) onto a named slot on the 65" Samsung Frame TV (Matte Display).
> - **Safety**: Only projects when the TV is already on and in Art Mode (`tv_mode == "art"`). Never wakes a sleeping TV or interrupts TV/movie viewing.
> - **Two Usage Modes**:
>   1. *Trompe-l'œil Wall Art / Ambient Signals*: Generate a `3840x2160` (`16:9`) image and use `optical_grade="matte_natural"` (default). Follow Matte Display optical laws: never draw fake outer frames or deep 3D shadowboxes; avoid large contiguous areas with `RGB < 22` (which glow gray on LCD); use 0.5–3mm tactile matte textures (impasto, lime plaster, rag paper, 1:1 macro crops of 8000px+ museum art, penumbra shadows); keep any text `<4%` of canvas area disguised as a physical label/seal. Use `dry_run=True` first and `read` the returned `512x512` `retinal_crops` to verify 1:1 pixel quality before uploading.
>   2. *General 65-Inch Display ("大屏看图" for diagrams, charts, screenshots, photos)*: Pass any aspect ratio with `fit="contain"` (scales without cropping and pads with a matching paper/slate mat) and set `optical_grade="raw"` if exact UI/chart colors must be preserved (or `"matte_natural"` for soft paper-like reading in a dim room). Set `ttl_sec` (e.g. `900`) for temporary viewing so the wall automatically reverts afterward.

- **Arguments**:
  - `image_path: str`
  - `slot: str = "main"` (`^[a-zA-Z0-9_-]{1,32}$`)
  - `show: bool = True`
  - `fit: Literal["contain", "cover", "pad_paper", "pad_slate"] = "contain"`
  - `optical_grade: Literal["matte_natural", "raw"] = "matte_natural"`
  - `dry_run: bool = False`
  - `summary: str | None = None`
  - `metadata: dict[str, Any] | None = None`
  - `ttl_sec: int | None = None` (`10 .. 86400`)
  - `owner: str | None = None`
  - `request_id: str | None = None`
  - `host: str | None = None`
- **Returns**:
  ```json
  {
    "request_id": "req_01",
    "duid": "uuid:c142370e-b312-44d5-9b35-090b08162efd",
    "status": "projected | staged | dry_run | blocked_by_tv_mode | staged_only_tv_left_art_mode | staged_only_user_override_during_upload | outcome_unknown",
    "tv_mode": "art",
    "projected": true,
    "staged": true,
    "slot": "main",
    "revision": 4,
    "content_id": "MY_F0207",
    "observed_current_id": "MY_F0207",
    "cached_image_path": "/Users/tefx/.local/share/frame-art-mcp/.../blobs/main__r4.jpg",
    "preview_full_path": "/Users/tefx/.local/share/frame-art-mcp/.../previews/9f8e7d6c5b4a_full.jpg",
    "content_bbox": [0, 0, 3840, 2160],
    "mat_applied": "none | paper | slate",
    "optical_diagnostics": {
      "input_resolution": [3840, 2160],
      "input_aspect_ratio": 1.7778,
      "fit_applied": "contain",
      "optical_grade_applied": "matte_natural",
      "pre_lut": {"max_dark_blob_pct": 0.2, "glare_pct": 0.0, "neon_gamut_pct": 0.0},
      "post_lut": {"max_dark_blob_pct": 0.0, "glare_pct": 0.0, "neon_gamut_pct": 0.0},
      "synthetic_flatness_pct": 4.1,
      "transition_delta_pct": 11.2,
      "warnings": [],
      "retinal_crops": {
        "detail_crop_path": "/Users/tefx/.local/share/frame-art-mcp/.../previews/9f8e7d6c5b4a_detail_512.png",
        "tonal_crop_path": "/Users/tefx/.local/share/frame-art-mcp/.../previews/9f8e7d6c5b4a_tonal_512.png"
      }
    },
    "pending_delete_count": 0
  }
  ```

### Tool 3: `frame_switch` (`read_write`)
Switch to a pre-staged slot (device switch takes ~50ms once safety gate is verified) or start an in-session timed sequence across pre-staged slots.
- **Arguments**:
  - `target: str | list[SequenceStep]` — Either a slot name `"main"` or `[{"slot": "s1", "dwell_ms": 1000}, {"slot": "s2", "dwell_ms": 1000}]` (`1 <= len <= 32`, `250 <= dwell_ms <= 3600000`).
  - `loop: int = 1` (`1 .. 100`)
  - `restore_on_done: bool = False`
  - `request_id: str | None = None`
  - `host: str | None = None`
- **Returns**:
  ```json
  {
    "request_id": "req_02",
    "duid": "uuid:c142370e-b312-44d5-9b35-090b08162efd",
    "status": "switched | sequence_started | blocked_by_tv_mode | slot_not_found | outcome_unknown",
    "tv_mode": "art",
    "control_generation": 13,
    "displayed": {"slot": "s1", "revision": 1, "content_id": "MY_F0208", "cached_image_path": "..."},
    "observed_current_id": "MY_F0208",
    "active_sequence": null
  }
  ```

### Tool 4: `frame_clear` (`read_write`)
Cancel active sequences, optionally restore `baseline_content_id`, and garbage-collect owned `MY_Fxxxx` images (immediately if `tv_mode == "art"`, or queued in `pending_delete_ids` without touching the TV if `tv_mode in ("tv", "off", "unknown")`).
- **Arguments**:
  - `slots: list[str] | None = None` — Specific slots to clear (`None` clears all slots and cancels any sequence).
  - `restore_baseline: bool = True` — If `True` and clearing the currently displayed slot, switches back to the most recent remaining slot in `display_history` or `baseline_content_id` before deleting the cleared slot's `MY_Fxxxx`. If `False` (or if `baseline_content_id` is unavailable), retains the currently displayed `MY_Fxxxx` on screen (`cleanup_pending`) rather than deleting the active image.
  - `request_id: str | None = None`
  - `host: str | None = None`
- **Returns**:
  ```json
  {
    "request_id": "req_03",
    "duid": "uuid:c142370e-b312-44d5-9b35-090b08162efd",
    "status": "cleared | deferred_offline_clear | cleanup_pending_active_display | partial_clear | outcome_unknown",
    "tv_mode": "art",
    "control_generation": 14,
    "cleared_slots": ["main"],
    "restored_to_content_id": "SAM-S1110760",
    "observed_current_id": "SAM-S1110760",
    "deleted_content_ids": ["MY_F0207"],
    "pending_delete_ids": []
  }
  ```

### Resource 1: `frame://guide/matte-optics`
Returns the Matte Display Optical Laws and 4 self-contained Python/NumPy/SciPy recipes (Frequency-Separated Relief Transfer, Optical Penumbra Shadow Projection, Deckle-Edge Torn Paper / Intaglio Plate Mask, and IIIF Museum 4K 1:1 Crop Fetcher).
