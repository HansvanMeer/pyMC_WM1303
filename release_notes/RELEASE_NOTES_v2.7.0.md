# Release Notes — v2.7.0

**Release date:** 2026-07-22
**Type:** Minor release — restores upstream frontend compatibility on WM1303 devices

---

## Overview

This release closes a compatibility gap that appeared after a fork-side update of the upstream repeater UI. The newer web UI calls several backend endpoints that were missing from the current backend fork, causing configuration save, service restart, and the Observer/broker templates dropdown to fail on WM1303 devices.

Six functional fixes are shipped, all applied through the WM1303 overlay layer — the upstream forks are not modified. The install and upgrade scripts have been updated so both new installations and in-place upgrades pick up all fixes automatically.

---

## Highlights

### Backend endpoints (added to the overlay)

Six `@cherrypy.expose` methods that the newer UI calls but the fork backend was missing were ported 1:1 from upstream into `overlay/pymc_repeater/repeater/web/api_endpoints.py`. Without these, the SPA catch-all served `index.html` in place of JSON, breaking multiple UI tabs.

| Endpoint | Purpose |
|---|---|
| `GET /api/validate_config` | Config preflight run before every service restart (returns `valid`, `errors`, `warnings`, `blocked_restart`) |
| `GET /api/broker_presets` | Observer templates dropdown (LetsMesh, Meshat.se, MeshCore.CA, MeshMapper, Waev) |
| `GET /api/site_info` | Site name / branding info |
| `GET /api/packet_by_hash` | Packet lookup by hash |
| `GET /api/packet_by_id` | Packet lookup by row id (plus new `get_packet_by_id` proxy in `storage_collector.py`) |
| `GET /api/policy_groups` | Policy group overview |
| `GET /api/policy_group_entries` | Policy group contents |

**WM1303 adaptation in `validate_config`:** the upstream validator did not recognise `radio_type: wm1303` and would report `valid:false, blocked_restart:true` on every WM1303 device. The overlay adds `wm1303` to `known_radio_types` and skips the `radio.*` / `sx1262` checks for that type, because WM1303 radio parameters live in `wm1303_ui.json` rather than the `radio:` section of `config.yaml`.

### Service restart hardening (race-condition fix)

The repeater runs as a non-root system user. When the backend invoked `sudo systemctl restart <service>` synchronously, systemd tore down the running process (and the sudo child in its process tree) before `subprocess.run` could return cleanly. The result was a non-zero return code with an empty `stderr`, surfacing to the UI as *"Restart failed: Unknown error"* even though the underlying restart command was valid.

`restart_service` in the overlay is now fire-and-forget:

- The HTTP response returns success immediately, before SIGTERM arrives.
- A daemon thread spawns a detached `subprocess.Popen(..., start_new_session=True)` that runs `sudo -n systemctl restart <service>` after a short delay.
- Because the child runs in its own session, it survives the parent going away and completes the restart normally.

### Frontend save-form hardening (Observer/MQTT tab)

Two upstream frontend issues in the built UI assets caused the Observer/MQTT save flow to fail or silently drop settings:

1. The broker load mapping defaulted `port` to `0`, so any partially completed broker was rejected server-side with a generic "missing required field: port" error.
2. The form read `disallowedInput` when saving, but the backend uses `disallowed_packet_types` — the field silently disappeared when a broker was edited.

Fixes:

- **Backend hardening** in `update_mqtt_config`: port is validated separately with a clear per-broker message (`Broker '<name>': port is required (1-65535)`); optional fields (`disallowed_packet_types`, `transport`, `tls`, `base_topic`) are tolerated instead of causing a hard failure.
- **Build-independent JS patch** `_tools/patch_ui_observer_save.sh`: an idempotent post-build script that patches the built assets (`repeater/web/html/assets/*.js`, hash-named), replacing `port??0` → `port??1883` and `disallowedInput` → `disallowed_packet_types`. It creates a `.pre_observer_patch` backup and is safe to run repeatedly.

### Channel E/F bridge import regression

A prior refactor accidentally placed the `resolve_config_path` import inside the module docstring in both `channel_e_bridge.py` and `channel_f_bridge.py`. The import therefore never actually executed, and the module-level path resolution raised `NameError` at load time. The result was that `ChannelEBridge` and `ChannelFBridge` silently failed to load, the backend RX callback was never registered, and every channel_e/channel_f packet was dropped — leading to an entirely empty UI (zero counters, blank graphs, no neighbours) after upgrading through the affected version.

The imports are now in the real import block in both files. After the fix, the RX callbacks register correctly, packets are routed to the appropriate channels, and all `/api/*` endpoints feeding the UI return live data again.

### Install / upgrade script updates

- New copy block for `overlay/pymc_repeater/repeater/presets/*.yaml` — required so `/api/broker_presets` can serve the built-in Observer templates on fresh installs and upgrades.
- New invocation of `_tools/patch_ui_observer_save.sh` after the web/html deploy in both `install.sh` and `upgrade.sh`.
- All existing overlay copy steps preserved.

---

## Upgrade

Run the standard upgrade command from the project root on the target device. The upgrade script:

1. Pulls the latest project revision.
2. Re-applies all overlay files (backend endpoints, storage helpers, bridges).
3. Copies the new `presets/*.yaml` templates.
4. Runs the Observer save-form patch against the currently deployed UI assets (idempotent).
5. Restarts the service.

No configuration changes are required. Existing `config.yaml` and `wm1303_ui.json` are preserved unchanged.

---

## Compatibility

- SenseCAP M1 / Raspberry Pi OS (Bookworm and Trixie).
- WM1303 HAT (SX1302 + SX1261).
- Existing v2.6.x installations upgrade in place.
