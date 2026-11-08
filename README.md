# frame-art-mcp

A standalone Python 3.11+ stdio MCP server for Samsung The Frame TVs. Four tools
share one safety gate, a crash-safe ownership ledger, and a 3840 × 2160 optical
conditioning pipeline. It supports ambient artwork and temporary diagrams,
charts, screenshots, document rasters, and photographs without stretching them.

## Installation

```sh
uv sync
uv run frame-art-mcp
# Or install the console command:
uv tool install .
```

Configure your MCP client to launch `frame-art-mcp`, or use
`uv --directory /path/to/frame-art-mcp run frame-art-mcp`. The process speaks MCP
on stdin/stdout; do not use stdout for application logging. `mcp[cli]` is pinned
to SDK major version 1 because this server uses its FastMCP API.

No preset IP is required. Discovery verifies Samsung Frame metadata on port
8001, attempts the bound device's cached IP (0.6 s), searches SSDP (1 s), then
probes the active private IPv4 /24 with 48 concurrent requests (0.4 s each,
2.5 s aggregate budget). First-run binding requires exactly one Frame TV.
Multiple candidates require an explicit identity choice. Relocation never
replaces an established DUID with another TV.

Optional environment variables:

| Variable | Meaning |
| --- | --- |
| `FRAME_TV_DUID` | Global identity constraint, including explicit host overrides |
| `FRAME_TV_HOST` | Explicit IP/hostname override, still checked against any DUID binding |
| `FRAME_ART_STATE_DIR` | State root; defaults to `~/.local/share/frame-art-mcp` |

A tool's `host` argument applies to that call and never overwrites automatic
binding. Discovery errors include `target_unavailable`, `duid_mismatch`,
`ambiguous_multiple_tvs`, `discovery_incomplete`, and `device_cache_invalid`.
An incomplete first-run subnet scan cannot establish a binding.

## Hardware safety and pairing

Projection requires an already-awake TV in Art Mode. The server never sends
remote keys, wake commands, or commands that enable Art Mode. Every mode probe
combines authoritative HTTP `PowerState` with Art WebSocket `get_artmode()`:
standby is `off` even if the WebSocket reports a stale `on`; failures become
`unknown`. All upload, select, and delete operations require `art` mode.

`frame_inspect` performs no device writes and does not initiate pairing. If the
TV requires token authentication and the local token is missing, empty, or
rejected, inspection reports `pairing_required`. The first authorized write
may require approval on the television; approve it manually and retry if its
outcome is unknown. Pairing tokens remain local in the per-device `token.txt`.
The WebSocket uses Samsung's local TLS interface on port 8002; the upstream
library does not verify the television's self-signed certificate.

`frame_put` checks Art Mode before upload and checks mode plus the pre-upload
current artwork again before selection. If the viewer changes mode or artwork
during upload, the new artwork remains staged and the screen stays untouched.
The two checks reduce race windows; the TV protocol offers no atomic
compare-and-select primitive.

## Architecture and durable ownership

- `discovery.py`: identity-bound discovery and device storage layout.
- `optics.py`: local decoding, scaling, conditioning, diagnostics, and previews.
- `ledger.py`: schema v3 state, ownership, WAL recovery, and reconciliation.
- `tv_client.py`: synchronous Samsung adapter and one serialized thread per DUID.
- `server.py`: MCP tools, resource, active TTL timers, and timed sequences.
- `guide.py`: optical laws and four executable 15-line recipes.

Each device has `ledger.lock`, `ledger.json`, `token.txt`, `blobs/`, and
`previews/` under a sanitized DUID directory. A synchronous worker acquires and
releases `fcntl.flock` inside its transaction. Cancelling the awaiting tool
cannot release a lock while socket I/O is still executing. Shutdown drains
in-flight work before closing the executor. This implementation targets Unix
systems with `fcntl`.

Ledger commits write `ledger.json.tmp.<pid>`, fsync the file, atomically replace
the ledger, and fsync the directory. Corrupt, incompatible, or mismatched
ledgers move to `ledger.corrupt.<timestamp>.json`; recovery starts with no owned
IDs and never infers ownership from filenames or the TV's personal album.

Only IDs actually returned by a successful upload enter `owned_ids`. An upload
whose reply was lost leaves a WAL record: later album differences become
`unattributed_candidates` for manual review, never automatic deletion targets.
Interrupted selects reconcile against the actual screen before another write.
Uncertain requests are reconciled without replaying their effects. Reusing a
`request_id` with changed parameters returns `request_id_conflict`; terminal
responses are cached locally.

Authorized writes reconcile four stages: observation and human override;
expiry of staged and displayed revisions; safe screen eviction using the most
recent valid display-history entry or baseline; and garbage collection with a
fresh mode/current-image guard before each delete. Baseline, current, displayed,
expected, and staged IDs remain protected. Deletes require confirmed ownership
and never delete the baseline. A failed delete stays pending until confirmed.
Changing artwork through the TV UI invalidates active sequence control while
preserving managed slots and protecting the viewer's chosen artwork.

Staging a replacement revision does not overwrite the currently displayed
revision's cache or TTL. Offline clears update local intent and defer TV
cleanup. With `restore_baseline=False`, an active image remains protected until
the viewer or a later write replaces it. If no fallback exists, cleanup stays
pending rather than deleting the active artwork.

## Five-stage Matte Display optical conditioning

1. **Decode**: reject inputs above 120 million pixels and animated/multipage
   images; apply EXIF orientation; convert ICC/CMYK/16-bit input to float32 sRGB.
   Transparent diagrams composite onto paper or slate according to foreground
   luminance and the selected fit.
2. **Scale**: Lanczos scaling preserves aspect ratio. `contain`/`pad_paper`/
   `pad_slate` center the image without cropping; `cover` center-crops to 16:9.
3. **Condition**: compute content-only pre-LUT diagnostics. `matte_natural`
   uses the specification’s quadratic C1-continuous shadow toe-lift and highlight
   roll-off, then softly compresses HSV saturation above 0.88;
   `raw` preserves scaled content values.
4. **Frame and encode**: use warm paper or mineral slate without fake bevels
   or shadow lines; deterministic input-seeded triangular ±1 LSB dither applies
   to conditioned content and mat margins. Raw content is not dithered. Output
   is 3840 × 2160 JPEG, quality 96, 4:4:4. Dry-run and upload use identical bytes.
5. **Diagnose**: content-scoped 8-connected dark-blob measurements, pre-dither
   Laplacian flatness, neon saturation, glare, transition delta, and lossless
   native-pixel detail/tonal crops. Crops use maximum Sobel-energy/minimum-mean
   luminance windows, stay inside the content inset, and never upscale. A
   content dimension below 64 px skips crops without blocking projection.

Metrics describe sRGB signal heuristics, not measured panel luminance. Physical
appearance also depends on ambient light, TV brightness, and viewing distance.
Previews are an mtime-pruned cache of at most 16 files. Historical preview paths
can expire; immutable managed revision JPEGs live separately in `blobs/`.
Transition delta compares decoded output frames when a displayed revision cache is available, including after restart.

## MCP reference

### `frame_inspect(include_optics_guide=False, host=None)`

Read-only device inspection: identity, resolution, mode, safety, observed and
expected content, baseline, slots, displayed revision, generation, pending
cleanup, sequence, and unattributed candidates. `include_optics_guide=True`
returns the full guide even through tool-only bridges. Expired persisted slots
are visible through their expiry timestamps; inspection never deletes them.

### `frame_put(image_path, slot="main", show=True, fit="contain", optical_grade="matte_natural", dry_run=False, summary=None, metadata=None, ttl_sec=None, owner=None, request_id=None, host=None)`

Use `raw` for color-exact UI/chart content and `matte_natural` for ambient art.
`show=False` stages without selecting. `dry_run=True` writes only local previews
and diagnostics, including retinal crops; it can render even when no TV is
reachable. Slots match `[a-zA-Z0-9_-]{1,32}`. TTLs range from 10 to 86400 seconds.
Inspect the crops before using `show=True`. Mat choices are `contain`, `cover`,
`pad_paper`, and `pad_slate`.

Typical statuses: `projected`, `staged`, `dry_run`, `blocked_by_tv_mode`,
`staged_only_tv_left_art_mode`, `staged_only_user_override_during_upload`, and
`outcome_unknown`. Diagnostics include the content box, chosen mat, pre/post
metrics, flatness, warnings, transition delta, and retinal crop paths.

### `frame_switch(target, loop=1, restore_on_done=False, request_id=None, host=None)`

Select a slot or sequence such as
`[{"slot":"a","dwell_ms":1000},{"slot":"b","dwell_ms":1000}]`.
Sequences accept 1–32 steps, 250–3600000 ms per dwell, and 1–100 loops.
Expired targets return `slot_expired`. Every background step checks the control
generation, mode, and expected artwork before selection or final restoration.
A manual put/switch/clear, viewer override, mode departure, or server shutdown
terminates the sequence. Persisted sequences never auto-resume after restart.

### `frame_clear(slots=None, restore_baseline=True, request_id=None, host=None)`

Cancel sequences and clear selected slots, or all slots with `None`. Restore a
valid remaining history entry or baseline before deleting active owned art.
Offline cleanup remains queued. Responses distinguish `cleared`,
`deferred_offline_clear`, `cleanup_pending_active_display`, `partial_clear`,
and `outcome_unknown`.

### `frame://guide/matte-optics`

Matte Display Optical Laws and self-contained 15-line recipes for
Frequency-Separated Relief Transfer, Optical Penumbra Shadow Projection,
Deckle-Edge Torn Paper / Intaglio Plate Mask, and IIIF Museum 4K 1:1 Crop Fetcher.
The IIIF recipe performs network I/O only when a user explicitly executes it;
the resource itself is static.

## Process lifetime and verification

Expiry timestamps use Unix seconds; `created_at` uses ISO 8601 UTC. The worker
and background scheduler share an injectable clock; the scheduler also accepts an
injectable sleeper for deterministic time-boundary tests.

This server needs no daemon. TTL timers and sequences run only while its stdio
process lives. On restart, expired intent persists; the next authorized write
in Art Mode evicts and garbage-collects expired revisions. TTL timers are tied
to `(slot, revision)`, so replacing a staged slot cannot cancel an older
currently displayed revision's expiry.

```sh
uv run pytest -q
uv run python -c "import frame_art_mcp.discovery, frame_art_mcp.optics, frame_art_mcp.ledger, frame_art_mcp.tv_client, frame_art_mcp.server"
```

Tests use temporary state directories and injected HTTP/TV protocol doubles.
A socket guard rejects accidental real-network connections and SSDP broadcasts.
They do not verify live firmware compatibility, physical display appearance,
or a television's pairing UI. No unit test contacts a real LAN television.
