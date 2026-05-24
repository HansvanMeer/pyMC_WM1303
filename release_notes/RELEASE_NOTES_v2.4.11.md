# Release Notes — pyMC_WM1303 v2.4.11

**Release date**: 2026-05-24
**Type**: Patch release — critical bug fixes + UX improvements
**Upgrade**: Recommended for all v2.4.10 users

---

## 🚨 Critical fix — TRACE echo-filter bug

v2.4.10 introduced a regression where **all pings were silently discarded** as `unknown_echo`. This affected both TCP companion pings and dashboard-initiated pings, on **all regions**.

### Root cause

In `wm1303_backend.py`, the self-echo guard checked the wrong bits of the MeshCore packet header:

```python
# v2.4.10 (broken):
if data[0] != 0x09:  # treats whole byte as TYPE
    self._tx_echo_hashes[_tx_hash] = time.monotonic()
```

Byte 0 of a MeshCore packet encodes `[VER(2)|TYPE(4)|ROUTE(2)]`, so TYPE lives in **bits 5–2**, not the whole byte. For a TRACE packet (TYPE=9, ROUTE=2), `data[0] = 0x26`, which never equals `0x09`. The guard never triggered, TRACE TX hashes were always cached, and TRACE_RESP packets coming back from neighbours matched those hashes → discarded as `unknown_echo` → ping never completed.

### Fix

```python
# v2.4.11 (correct):
_tx_type = (data[0] >> 2) & 0x0F if len(data) > 0 else 0
if _tx_type != 0x09:
    self._tx_echo_hashes[_tx_hash] = time.monotonic()
```

Applied at both echo-cache sites:
- `_send_for_enqueue()` (called when packets enter the TX queue)
- `_send_for_scheduler()` (called when the scheduler hands packets to pkt_fwd)

Credit: **@fahimshariff-au** (issue #7, commit reference in his fork).

---

## 🚨 Critical fix — TRACE dispatch missing in WM1303 bridge flow

After applying the echo-filter fix above, deeper investigation on pi01 revealed a **second, independent regression** in the WM1303 bridge flow: incoming TRACE pings from companion nodes were never dispatched to `TraceHelper.process_trace_packet()`. As a result the repeater silently re-broadcast the original TRACE without appending its own SNR, so companions never received the trace data and pings appeared to time out.

### Root cause

The upstream pymc_repeater router (`packet_router.py::_route_packet`) dispatches TRACE packets to `TraceHelper.process_trace_packet()` before the engine forwards them:

```python
if payload_type == TraceHandler.payload_type():
    elif self.daemon.trace_helper:
        await self.daemon.trace_helper.process_trace_packet(packet)
        processed_by_injection = True  # skip generic engine forward
```

The WM1303 bridge path bypasses that router entirely: RF packets arriving on channel E/F go through `BridgeEngine` → `BridgeRepeaterHandler._bridge_repeater_handler` → `repeater_handler.process_packet`. There was no TRACE-specific dispatch in `_bridge_repeater_handler`, so TRACE packets fell through to the generic forwarding path.

### Fix

Added a TRACE branch in `overlay/pymc_repeater/repeater/main.py::_bridge_repeater_handler`, placed right after the existing ADVERT block and before the generic `process_packet()` call:

```python
if payload_type == TraceHandler.payload_type() and self.trace_helper:
    _saved_injector = self.trace_helper.packet_injector
    async def _bridge_trace_injector(fwd_packet, wait_for_ack=False):
        fwd_bytes = fwd_packet.write_to()
        if self.bridge_engine:
            await self.bridge_engine.inject_packet(
                'repeater', fwd_bytes, origin_channel=origin_channel
            )
    self.trace_helper.packet_injector = _bridge_trace_injector
    try:
        await self.trace_helper.process_trace_packet(pkt)
    finally:
        self.trace_helper.packet_injector = _saved_injector
    return
```

Key design points:
- We **reuse the existing `TraceHelper`** instance (already created in main.py line 266) — no duplication of TRACE parsing, hop matching, or completion logic.
- We **temporarily override `packet_injector`** so the SNR-annotated TRACE forward goes through `BridgeEngine.inject_packet('repeater', …)` (which feeds channel E/F TX queues) instead of `router.inject_packet` (which only knows the classic radios[0]/[1] TX path that channel E/F don't use).
- `origin_channel` is preserved so the bridge engine's origin-channel-first priority still works.
- The `try/finally` guarantees the injector is restored even on exceptions.

### Why this fix lives in WM1303 overlay (not upstream)

The TRACE dispatch gap exists only in the WM1303 bridge flow, which is a WM1303-overlay feature on top of upstream pymc_repeater. Fixing it in `main.py` overlay keeps the change scoped to this fork and doesn't require upstream coordination.

---

## ⚠️ Config schema migration — region as plain string

v2.4.10 expects the `region` key in `/etc/pymc_repeater/wm1303_ui.json` to be a **nested dict**:

```json
{"region": {"code": "EU868", "tx_freq_min": null, "tx_freq_max": null}}
```

Upgrading from an older install where `region` was a plain string (e.g. `"region": "EU868"`) caused a startup error. The fix is now automatic: `_load_ui()` in `wm1303_api.py` detects legacy formats and migrates them on first read, then persists the canonical schema back to disk.

Handled cases:
- `"region": "EU868"` → `{"code": "EU868", "tx_freq_min": null, "tx_freq_max": null}`
- Missing `region` key → empty nested dict (no more KeyErrors in downstream code)

Credit: **@fahimshariff-au** (issue #7).

---

## 📝 Documentation — non-interactive bootstrap now in README

The `WM1303_REGION` environment variable was supported by `bootstrap.sh` since v2.4.9 but only documented in the script header itself. v2.4.11 adds an **Advanced installation** section to the README showing:

```bash
curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo WM1303_REGION=AU915 bash
```

Supported codes: `EU868`, `US915`, `AU915`, `AS923`, `IN865`, `JP920`, `KR920`, `CUSTOM`. When `WM1303_REGION` is set, the bootstrap selects the matching channel preset automatically and skips the interactive wizard — ideal for scripted / headless deployments.

---

## 🆕 Companion App Compatibility (post-release additions)

Field testing on pi01 with a MeshCore-family companion app revealed several gaps in how the WM1303 bridge flow served companion requests. The following companion-facing fixes were added on top of the initial v2.4.11 release, all bundled under the same version tag.

### Bridge-aware response injector for helpers (`main.py`)

Upstream `LoginHelper`, `TextHelper`, and `ProtocolRequestHelper` are constructed with `packet_injector=self.router.inject_packet`. That injector targets `radios[0]/[1]` (the classic-radio TX path), which is **not active** in a WM1303 deployment — the WM1303 only has channel E (SX1261) and channel F (SX1302 service modem) available. As a result, helper-generated responses (login success, CLI replies, status/telemetry packets) silently never reached the air.

A per-request override-then-restore pattern would race against `asyncio.create_task(_delayed_send(...))` used by LoginHelper, so a **permanent bridge-aware injector** was introduced:

```python
async def _response_injector(self, packet, wait_for_ack: bool = False):
    """Bridge-aware injector: route helper responses via channel E/F."""
    packet_bytes = packet.write_to()
    if self.bridge_engine is not None:
        await self.bridge_engine.inject_packet('repeater', packet_bytes)
        return
    # Fall back to upstream router only if bridge is not configured.
    await self.router.inject_packet(packet, wait_for_ack=wait_for_ack)
```

All three helpers are now constructed with `packet_injector=self._response_injector`. The bridge engine's standard origin-channel + round-robin selection takes care of channel choice.

### `WM1303ProtocolRequestHelper` — telemetry + extended owner info

New file: `overlay/pymc_repeater/repeater/wm1303_telemetry_helper.py` (436 lines). Subclasses upstream `ProtocolRequestHelper` and adds:

1. **REQ_TYPE_GET_TELEMETRY_DATA (0x03)** handler with CayenneLPP payload:
   - Channel 1: CPU temperature (°C) — from `/sys/class/thermal/thermal_zone0/temp`
   - Channel 2: WM1303 concentrator temperature (°C) — from `/tmp/concentrator_temp` (written by pkt_fwd)
   - Channel 3-5: CPU usage / memory usage / disk usage as **Humidity-type** values (so the companion displays them as readable `XX %` rather than confusing `0.0 V` / `Analog Input N`)

2. **Extended OWNER_INFO (REQ 0x07) response** — appends device hardware lines after the standard firmware/name/owner regels. Kept for any future companion that switches to the binary REQ 0x07 path; current companions use the CLI `get owner.info` path (see next section).

3. **`FIRMWARE_VER_LEVEL = 11` monkey-patch** applied to `pymc_core.node.handlers.login_server` at import time. The MeshCore companion gates the Owner Info menu on `firmware_ver_level ≥ 2`; upstream pymc_repeater reports `1`, which caused the companion to display `"this repeater requires a firmware update to enable owner info"`. Setting `11` unblocks Owner Info, telemetry refresh, and other gated features without changing upstream pymc_repeater code.

`main.py` imports `WM1303ProtocolRequestHelper` with a graceful fallback so the upstream helper is used when this overlay is absent:

```python
try:
    from repeater.wm1303_telemetry_helper import WM1303ProtocolRequestHelper
except ImportError:
    WM1303ProtocolRequestHelper = ProtocolRequestHelper
```

### `mesh_cli.py` — dynamic `owner.info` CLI handler

New file: `overlay/pymc_repeater/repeater/handler_helpers/mesh_cli.py` (856 lines, full overlay of upstream `mesh_cli.py`).

**Surprise discovery**: the MeshCore companion does **not** call binary REQ 0x07 (GET_OWNER_INFO) at all. It sends a `TXT_MSG` CLI command `get owner.info` over the encrypted admin channel. Upstream's `_cmd_get` doesn't know that key and returns the literal string `??: owner.info`, which the companion then displays verbatim on its Owner Info page.

*(There is also a `repeater_cli.py` file in the upstream tree that contains an identical `MeshCLI` class, but `text.py` actually imports from `mesh_cli.py`. Patching `repeater_cli.py` has zero effect — a subtle trap worth knowing.)*

Fix:

1. **Module-level helper** `_build_dynamic_owner_info(max_len=115)` builds the value at request time from runtime sources:
   - Software version ← `/etc/pymc_repeater/version`
   - Hardware model ← `/sys/firmware/devicetree/base/model`
   - Total RAM ← `/proc/meminfo` (`MemTotal`)
   - Total disk ← `os.statvfs('/')` (root filesystem)
   
   Fields are joined with `|` (the companion's line separator) and **hard-capped at 115 characters** with graceful degradation — if appending a field would exceed the cap, it (and any following fields) is skipped rather than truncated mid-token.

2. **`get owner.info`** returns the dynamic string. Example output on a Pi 4 / 4 GB / 64 GB SD:
   ```
   pyMC_WM1303 v2.4.11|Raspberry Pi 4 Model B Rev 1.4|RAM: 3846 MiB|Disk: 58.2 GiB
   ```
   (79 / 115 chars, displays as four lines in the companion.)

3. **`set owner.info <value>`** returns `Error: owner.info is dynamic and read-only` — the value is derived from the actual device, so accepting a companion-side override would be misleading.

This approach automatically reports correct hardware/version info on **every install** without any per-device config edits.

---

## 📋 Known issues / not in this release

- **Spectrum scan regio-aware UI** (issue #7 comment #5, AU-specific): The scan range and heading still show EU868 defaults on AU915/US915 installs. The backend scan itself works; only the UI labels are off. **Targeted for v2.4.12.**
- **NF display spike-rejection + closest-scan-point** (issue #7): Quality improvement, not a regression. **Targeted for v2.4.12.**
- **VirtualLoRaRadio plain instance attributes** (issue #10): Separate tracking issue.

---

## Files changed

| File | Change |
|---|---|
| `overlay/pymc_core/src/pymc_core/hardware/wm1303_backend.py` | TRACE bit-shift fix on 2 echo-cache sites |
| `overlay/pymc_repeater/repeater/main.py` | TRACE dispatch in `_bridge_repeater_handler` + new `_response_injector` (bridge-aware) used by LoginHelper / TextHelper / ProtocolRequestHelper; imports `WM1303ProtocolRequestHelper` with graceful fallback |
| `overlay/pymc_repeater/repeater/wm1303_telemetry_helper.py` | **NEW** — `WM1303ProtocolRequestHelper` with REQ_TYPE_GET_TELEMETRY_DATA (CayenneLPP), extended OWNER_INFO, and `FIRMWARE_VER_LEVEL=11` monkey-patch |
| `overlay/pymc_repeater/repeater/handler_helpers/mesh_cli.py` | **NEW** — full overlay of upstream `mesh_cli.py` adding `_build_dynamic_owner_info()` module helper, dynamic `get owner.info` CLI handler, and read-only `set owner.info` |
| `overlay/pymc_repeater/repeater/web/wm1303_api.py` | `_migrate_ui_config()` helper + auto-migrate on load |
| `README.md` | 'Advanced installation' section with `WM1303_REGION` examples |
| `VERSION` | 2.4.10 → 2.4.11 |
| `release_notes/RELEASE_NOTES_v2.4.11.md` | This file |

---

## Upgrade instructions

Standard upgrade — no manual steps required, migration runs automatically:

```bash
curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
```

After upgrade, hard-refresh the UI (Ctrl+Shift+R) to clear any cached assets.

---

## Testing performed

### Code review
- TRACE bit-shift fix vs MeshCore packet header spec
- `_migrate_ui_config()` for both legacy-string and missing-key cases
- TRACE dispatch flow vs upstream `packet_router._route_packet`
- AST/syntax checks of patched `main.py` and overlay `mesh_cli.py` pass
- Cross-check that `text.py` imports `from .mesh_cli import MeshCLI` (not `repeater_cli`)

### Smoke / field tests on a Pi 4 / 4 GB test unit (EU868, Channel E @ 869.618 MHz BW62.5 SF7 CR5)
- Service start, UI reachability, region API endpoint verified
- TraceHelper initialization confirmed (`Trace processing helper initialized`)
- Local identity hash registered correctly (`path_hash=CDB7 size=2 bytes`)
- `WM1303TelemetryHelper` registered (`hash=0xCD (with TELEMETRY and extended OWNER_INFO support)`)
- Companion login (ANON_REQ) handled with admin ACL granted
- Companion `REQ type=0x01 (GET_STATUS)` round-trip via channel E (`_response_injector` path) confirmed
- Companion `REQ type=0x03 (GET_TELEMETRY_DATA)` round-trip confirmed; CayenneLPP fields display correctly in the companion (temp °C, percentages as Humidity %)
- Companion CLI `get owner.info` over TXT_MSG returns dynamic 79-char string built from `/etc/pymc_repeater/version`, `/sys/firmware/devicetree/base/model`, `/proc/meminfo`, `os.statvfs('/')`
- Companion CLI `set owner.info <value>` returns `Error: owner.info is dynamic and read-only` and does not mutate state
- `FIRMWARE_VER_LEVEL=11` patch verified in-process (`import repeater.wm1303_telemetry_helper; import pymc_core.node.handlers.login_server as ls; ls.FIRMWARE_VER_LEVEL == 11`)

### Validated end-to-end on the test unit with a MeshCore-family companion (24-May-2026)
- TRACE ping (separate fix from the earlier bridge dispatch work) – completes and returns SNR
- Telemetry refresh – four CayenneLPP values populate in the companion
- Owner Info – displays four lines (version / hardware / RAM / disk) sourced live from the device, no static config edit required
- No spurious `path_len too large` / `Unsupported packet version: 2` errors after fresh re-login (those occur only when an old companion session key races a restarted service ACL — normal mesh hygiene applies)
