# Release Notes — v2.5.0

**Type:** Stable release
**Date:** 2026-05-25
**Compatibility:** pyMC_core main (1.0.12), pyMC_Repeater main (1.0.11.dev1), HAL v2.10

---

## Summary

v2.5.0 is a **minor version bump** that introduces three significant improvements:

1. **Main branch tracking** — the installation and upgrade scripts now track the `main` branch of pyMC_core and pyMC_Repeater instead of `dev`, providing a more stable and predictable update model.
2. **Extended Debug Export** — four new collector methods and 18 additional file checksums give comprehensive diagnostic capabilities for troubleshooting deployment issues.
3. **Fork-based update check** — the UI version check now queries the HansvanMeer fork instead of upstream, eliminating false-positive "Update Available" warnings caused by setuptools-scm version calculation differences between branches.

All three improvements were validated via a clean bootstrap test. No breaking changes. Drop-in upgrade from v2.4.12.

---

## Pre-release changelog (v2.4.9 — v2.4.12)

v2.5.0 is the first **stable release** after four pre-release versions. Below is a consolidated summary of all changes introduced since v2.4.8.

### v2.4.9 — Multi-region support & observability overhaul

- **Packet Tracing overhaul**: ground-up rewrite with stats dashboard, analytics, heatmaps, histograms, and playback modal. New path-hash echo classifier replaces legacy time-based heuristic.
- **Tiered metrics query system**: Hot/Warm/Cool/Cold tiers give reliable visibility up to 8 days back. Fixed retention aggregator counter inflation bug.
- **Multi-region regulatory support**: eight regional presets (EU868, US915, AU915, AS923, IN865, JP920, KR920, CUSTOM), wideband Channel F (`chan_Lora_std`, BW125/250/500), community preset catalog, and four new REST API endpoints. Resolves [#1](https://github.com/HansvanMeer/pyMC_WM1303/issues/1) (AU915 BW250) and [#4](https://github.com/HansvanMeer/pyMC_WM1303/issues/4) (hardcoded EU868 limits).
- **Bridge engine independence**: Channels E and F now operate fully without any A–D channel active. Three structural fixes ensure the bridge engine, TX queue, and RX callbacks function independently.
- **JWT security hardening**: all 14 bare `fetch()` calls in the UI migrated to the authenticated `api()` helper.
- Upstream overlay refresh (USB/TCP radio support from pymc_core dev), config template overhaul, and upstream tag sync for correct version resolution.

### v2.4.10 — Multi-region reliability & clean install experience

- **Presets v3**: restructured to contain only `region` and `rf_center_freq_mhz`; all channel configs removed from presets for a clean starting point.
- **Interactive bootstrap wizard via curl pipe**: self-reexec mechanism (`/dev/tty`) enables interactive region selection even when piped from `curl`.
- **Idle mode**: service gracefully handles zero channels configured — web UI starts on port 8000 and allows configuration without crashing.
- **chan_FSK disabled** by default (unused by MeshCore).
- **Mandatory channel names**: every channel now requires a user-defined name, displayed throughout the UI including bridge-rule dropdowns.

### v2.4.11 — Critical bug fixes & companion app compatibility

- **TRACE echo-filter fix** (critical): all pings were silently discarded as `unknown_echo` due to incorrect bit-field extraction in the MeshCore packet header. Fixed TYPE extraction to use correct bit-shift.
- **TRACE dispatch in bridge flow** (critical): incoming TRACE packets from companion nodes were never dispatched to the trace helper in the WM1303 bridge path. Added bridge-aware TRACE dispatch with correct channel injector.
- **Companion remote-admin**: fixed dispatch for ANON_REQ, TXT_MSG, and PROTOCOL_REQ packets. Introduced permanent bridge-aware response injector (`_response_injector`) to eliminate race conditions.
- **Dynamic owner.info**: generates Raspberry Pi model, RAM, disk, and WM1303 version at runtime (115-character limit for companion display).
- **Companion telemetry**: CPU temperature, memory usage, and disk usage reported via CayenneLPP format to the companion app.
- **Firmware version level**: bumped to level 11 for companion Owner Info button compatibility.

### v2.4.12 — Issue #7 final fixes & script hardening

- **Bug 1**: non-interactive bootstrap now displays a prominent warning showing defaulted region/preset/sync-word values and override instructions.
- **Bug 2**: region change in the UI now shows a banner warning that channel frequencies are not automatically updated.
- **Bug 3**: Channel F IF-range guard prevents pkt_fwd crash loops when the frequency falls outside the SX1302 standard-channel IF range.
- **Robust git update flow**: replaced `git checkout -- . && git pull` with `git fetch + git checkout + git reset --hard + git clean -fd` — idempotent and eliminates merge conflicts from overlay-modified tracked files.
- **Recursive overlay re-apply**: upgrade script now re-applies all overlays after git pull, preventing stale overlay files after upstream changes.

---

## 1. Main branch tracking

### What changed

Both `install.sh` and `upgrade.sh` now set:

```bash
CORE_BRANCH="main"
REPEATER_BRANCH="main"
```

Previously these were set to `dev`.

### Why

The upstream pyMC_core and pyMC_Repeater projects use a standard git flow where feature development happens on the `dev` branch and periodically gets merged into `main` via Pull Requests. The `main` branch only receives peer-reviewed, merged code.

Tracking `main` provides:

- **Stability**: WM1303 installations only receive changes that have been formally merged via PR.
- **Predictable updates**: Changes arrive in batches (PR merges) rather than as individual commits.
- **Correct version reporting**: The locally installed version now matches the `main` branch version, eliminating the cosmetic mismatch that caused false "Update Available" warnings.

### Impact on existing installations

Existing installations on the `dev` branch will be **automatically migrated** to `main` when `upgrade.sh` runs. The upgrade script uses `git checkout -B main origin/main` to force-create the local `main` tracking branch, even if the repository was originally cloned with `-b dev`.

Since `main` contains all `dev` changes (via PR merges), no functionality is lost. The `main` branch may lag behind `dev` by a few commits until the next PR merge — this is intentional and provides a stability buffer.

---

## 2. Extended Debug Export

The Debug Export (`/api/debug/export`) has been significantly expanded to provide better diagnostic capabilities.

### New collector methods

| Method | Purpose |
|---|---|
| `_collect_wm1303_state()` | Captures current WM1303 operational state: region, sync word, Channel E/F config, active preset, TX bounds, RF center frequency |
| `_collect_service_journal(n=200)` | Captures the most recent 200 lines of the `pymc-repeater` systemd journal |
| `_collect_bridge_conf()` | Captures the generated `bridge_conf.json` (the pkt_fwd configuration) |
| `_collect_overlay_summary()` | Reports which overlay features are detected as active: Channel F bridge, region config, telemetry helper, update endpoints, mesh CLI, Bug 2 banner, Bug 3 IF guard, HAL EXP patches |

### Extended version information

The `_collect_versions()` method now records per-repository:

- **Branch name** (e.g., `main` vs `dev`) — immediately reveals migration issues
- **Remote URL** — confirms whether the fork or upstream is being tracked
- **Ahead/behind count** relative to `origin/<branch>` — shows if local changes exist
- **Setuptools-scm calculated version** — the actual version string used by the UI
- **HAL EXP patch detection** — scans `loragw_sx1302.c` source for experimental patch markers (`EXP_V2`, `EXP_V3`, `EXP_V4`)

### Extended file checksums

The `check_files` list has been expanded from **22 to 40 files**, adding:

- `channel_f_bridge.py` (v2.4.9)
- `wm1303_telemetry_helper.py` (v2.4.11)
- `handler_helpers/mesh_cli.py` (v2.4.11)
- `update_endpoints.py` (v2.5.0)
- `region_config.py` (v2.4.9)
- `tiered_query.py` (v2.4.9)
- `packet_trace.py` (v2.4.10)
- `metrics_retention.py` (v2.4.10)
- `uniform_tracer.py` (v2.4.10)
- All HAL source files (`loragw_sx1302.c`, `loragw_hal.c`, `loragw_sx1261.c`, `loragw_spi.c`, `loragw_lbt.c`, `loragw_aux.c`, `sx1261_spi.c`)
- HAL header files (`loragw_sx1302.h`, `loragw_hal.h`, `loragw_sx1261.h`, `sx1261_defs.h`)

This allows instant detection of missing or outdated overlay files — which is exactly how the three deployment bugs below were discovered.

---

## 3. Fork-based update check

A new overlay file `overlay/pymc_repeater/repeater/web/update_endpoints.py` changes the UI version check from:

```python
GITHUB_OWNER = "rightup"    # upstream (redirects to pyMC-dev)
```

to:

```python
GITHUB_OWNER = "HansvanMeer"  # our fork
```

This eliminates the persistent "Update Available" false-positive that occurred because:

1. The upstream `main` branch has merge commits that don't exist on `dev`.
2. The UI's `_detect_channel_from_dist_info()` returns `None` and defaults to checking `main`.
3. Setuptools-scm calculates different version strings for `main` vs `dev` even when the code is identical.

By checking against our fork — which is kept fully in sync with upstream via tag-sync — the installed version and remote version will always match, because both are built from the same commit and branch.

---

## 4. Deployment robustness fixes

Three bugs were discovered during bootstrap testing, **all detected by the new Debug Export**:

### Bug A — Branch name inconsistency after upgrade

**Symptom**: After upgrading from `dev` to `main`, `git branch --show-current` still reported `dev` even though HEAD was correctly pointing at `origin/main`.

**Root cause**: `git checkout main` silently fails when the local `main` branch doesn't exist (the repository was cloned with `-b dev`). The subsequent `git reset --hard origin/main` correctly advances HEAD but doesn't change the branch pointer.

**Fix**: Replaced `git checkout "${branch}"` with `git checkout -B "${branch}" "origin/${branch}"` in `upgrade.sh`'s `update_repo()` function. The `-B` flag forces creation of the local tracking branch.

### Bug B — `update_endpoints.py` overlay not deployed

**Symptom**: After bootstrap, the update check still pointed at `rightup` instead of `HansvanMeer`.

**Root cause**: The new overlay file was not listed in the hardcoded file list in the `repeater/web/` deployment section of `install.sh` and `upgrade.sh`.

**Fix**: Added `update_endpoints.py` to the web-level file list in both scripts.

### Bug C — `wm1303_telemetry_helper.py` overlay not deployed

**Symptom**: Companion telemetry (CPU/memory/disk via companion app) silently failed on fresh installs.

**Root cause**: Same as Bug B — the file was missing from the hardcoded `repeater/` level file list.

**Fix**: Added `wm1303_telemetry_helper.py` to the repeater-level file list in both scripts.

### Additional fix — `handler_helpers/` directory

The `handler_helpers/mesh_cli.py` overlay was previously deployed by an implicit mechanism. Both scripts now explicitly create the `handler_helpers/` directory and copy `mesh_cli.py` into it, ensuring reliable deployment.

---

## Files changed

| File | Change |
|---|---|
| `VERSION` | `2.4.12` → `2.5.0` |
| `install.sh` | Branch variables `dev` → `main`; added `wm1303_telemetry_helper.py`, `update_endpoints.py`, `handler_helpers/mesh_cli.py` to overlay lists |
| `upgrade.sh` | Branch variables `dev` → `main`; `git checkout -B` fix; added same overlay files as install.sh |
| `overlay/pymc_repeater/repeater/web/debug_collector.py` | 4 new collector methods; `check_files` 22 → 40; extended `_collect_versions()` with branch/remote/scm/HAL info |
| `overlay/pymc_repeater/repeater/web/update_endpoints.py` | New overlay: `GITHUB_OWNER` changed from `rightup` to `HansvanMeer` |

---

## Upgrade instructions

### From v2.4.12 (recommended path)

```bash
# SSH into the device
sudo /opt/pymc_repeater/wm1303/upgrade.sh
```

The upgrade script will:
1. Switch repositories from `dev` to `main` branch (automatic migration)
2. Deploy all new overlay files including Debug Export extensions
3. Rebuild HAL if source changes are detected
4. Restart the service

### Fresh install

```bash
# Interactive (region selection wizard)
curl -fsSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash

# Non-interactive (specify region via environment variable)
WM1303_REGION=EU868 curl -fsSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo -E WM1303_REGION=EU868 bash
```

### Post-upgrade verification

After upgrading, verify the following in the WM1303 Manager UI (`http://<device-ip>:8000`):

1. **Hard refresh** the browser: Ctrl + Shift + R
2. **Version**: should show `v2.5.0`
3. **Update status**: should show "Up to Date" (no "Update Available" warning)
4. **System Status → Debug Export**: download and verify the expanded diagnostic data

---

## Known limitations

- The `_detect_channel_from_dist_info()` function in the upstream update check code returns `None` for editable (development) installs. The UI defaults to checking the `main` channel, which now correctly matches our installation branch. This is a cosmetic limitation of the upstream code that does not affect functionality.
- The overlay file deployment in `install.sh` and `upgrade.sh` still uses hardcoded file lists. A future improvement (tracked in TODO) is to migrate to recursive `rsync` or `find`-based copying to automatically pick up new overlay files without script changes.
