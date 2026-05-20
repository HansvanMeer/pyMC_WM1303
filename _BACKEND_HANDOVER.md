# Backend Handover — Multi-Region + Channel F Implementation

**Status:** Backend phases complete (NOT deployed, NOT committed, no version bump).
**Next:** UI phase in a new session.

## What's done (backend, all syntactically valid)

### New files
- `overlay/pymc_core/src/pymc_core/hardware/region_config.py` — 8 regions + CUSTOM, sync word constants, helper functions (get_tx_bounds, get_sx1261_calib, get_region_summary). 244 lines.
- `config/presets.json` — 8 channel preset templates (EU/US/AU/AS/IN/JP/KR + Custom). Each preset defines defaults for ALL channels A/B/C/D/E/F. Used by installation wizard only; NOT shown in Channels tab post-install.

### Modified files
- `overlay/pymc_core/src/pymc_core/hardware/wm1303_backend.py` — region_config import, region read from wm1303_ui.json, dynamic tx_freq_min/max via region, channel_f read + dynamic chan_Lora_std (enable + if-offset + BW + SF from UI).
- `overlay/pymc_core/src/pymc_core/hardware/sx1261_driver.py` — region-aware image calibration via _read_region_from_ui() helper.
- `overlay/pymc_core/src/pymc_core/hardware/__init__.py` — merged: keeps WM1303Backend + VirtualLoRaRadio, adds upstream USBLoRaRadio + TCPLoRaRadio.
- `overlay/pymc_repeater/repeater/config.py` — refreshed from upstream HEAD (432 → 567 lines), WM1303 elif branch preserved.
- `overlay/pymc_repeater/repeater/web/wm1303_api.py` — 4 new endpoints (dispatchers + implementations).
- `config/wm1303_ui.json` — template now has `region: {code:"EU868", ...}` and `channel_f: {enabled:false, ...}`.
- `install.sh` — copies region_config.py and presets.json during install/upgrade.

## Architecture decisions (locked)

1. **Region is DEVICE-WIDE** (top-level in wm1303_ui.json), NOT per-channel.
2. **All channels A/B/C/D/E/F available for every preset/region** — presets are starter defaults, user customizes via Channels tab afterwards.
3. **Channel F = chan_Lora_std on RF0** — runs in PARALLEL with channels A-D (chan_multiSF_0-3). BW125/250/500.
4. **Channel E (SX1261/SX1262) supports BW62.5/125/250/500** — was UI-restricted to BW62.5 only.
5. **Channels A-D = chan_multiSF_0-3, BW125 ONLY** (hardware constraint).
6. **Presets shown ONLY at install-time** (bootstrap.sh wizard), never in Channels tab post-install.
7. **No IF-chain locking needed** for BW>125 — channel F uses dedicated chan_Lora_std slot which is parallel to A-D.

## API endpoints (ready for UI to call)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/wm1303/regions` | GET | List all 8 regions + CUSTOM with metadata |
| `/api/wm1303/region` | GET | Current device region |
| `/api/wm1303/region` | POST/PUT | Update region (validates code, requires bounds for CUSTOM) |
| `/api/wm1303/presets` | GET | List 8 community presets (install-wizard data) |

## UI phase TODO (next session)

1. **REGION & REGULATORY block** in Channels tab — placed RIGHT of RF Center Frequency. Dropdown of regions, displays resolved tx_freq_min/max bounds, CUSTOM shows editable bounds. Calls `/api/wm1303/region`.
2. **Channel F card** in Channels tab — clone of channel_e card pattern, BW dropdown 125/250/500.
3. **BW dropdowns** — Channels A-D: BW125 only. Channel E: 62.5/125/250/500. Channel F: 125/250/500.
4. **TX Power dropdown** — all 16 LUT entries (12-27 dBm).
5. **Per-channel sync_word UI** — Private (0x1424) / Public (0x3444) / Custom (hex input).
6. **Installation wizard** in bootstrap.sh — interactive region + preset prompts, env var override support, writes region + preset to wm1303_ui.json.
7. **UI ↔ config parameter completeness audit** — ensure every field in UI also persists to wm1303_ui.json and propagates to bridge_conf.
8. **Deploy + test on pi01**.

## Resolves

- GitHub Issue #1 (mkwillis): AU915 BW250 support via Channel F.
- GitHub Issue #4: Hardcoded EU868 frequency limits replaced with region-aware bounds.

## Backup files

- `/tmp/config.py.overlay.bak` — old overlay config.py before refresh
- `/tmp/upstream_sync/` — upstream pymc_core and pymc_repeater clones
