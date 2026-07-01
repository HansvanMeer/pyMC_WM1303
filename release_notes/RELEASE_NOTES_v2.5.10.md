# v2.5.10 — Central Layer-2 protocol validator + Invalid Packets forensics

**Release date**: 2026-07-01
**Type**: Patch (bug fix + new forensics tab)

## Summary

Adds a central MeshCore Layer-2 protocol validator that drops structurally-invalid RF packets at the hardware ingress point, before they reach any consumer (radios, MQTT, companion). Malformed packets (foreign/misconfigured neighbour nodes, likely Meshtastic traffic on overlapping frequencies, and reserved `hash_size=4` path encodings) are now filtered and logged forensically in a new **Invalid Packets** UI tab. Also fixes a pre-existing HTTP 500 on `/api/stats` and improves the channel identification shown in the forensics view.

## Why

The repeater was forwarding all raw RF packets to the companion/discovery path (a side effect of an earlier node-discovery change), including packets that are not valid MeshCore frames. These pass CRC but have an unparseable path layout, so downstream consumers such as mc-radar misread the "pubkey" bytes and register each variation as a new unique source — producing hundreds of fantom-sources. A structural validator at the ingress point stops this at the root without affecting legitimate node discovery.

Separately, the root dashboard polls `/api/stats` every 60 s. A bytes field nested in the stats payload caused a recurring `TypeError: Object of type bytes is not JSON serializable`, surfacing as a persistent HTTP 500 "System Configuration Error".

## Changes

### 1. Central Layer-2 protocol validator (core fix)

Integrated `protocol_validator.validate_and_record()` into `WM1303Backend._dispatch_rx()` (file `overlay/pymc_core/src/pymc_core/hardware/wm1303_backend.py`), placed **after** the noise filter and **before** the pre-filter raw RX callbacks. Structurally-invalid packets are dropped there (with a warning log and a drop counter) so they never reach the raw RX callbacks, bridge inject, virtual radios, or the MQTT publish path. The check is purely structural (no crypto, sub-millisecond) and is wrapped in `try/except` so it can never block the RX path.

Drop reasons include: `reserved_path_len_hash_size_4`, `length_implausible`, `too_short`, `invalid_route_type`, `transport_code_length_mismatch`, `path_overflow`.

### 2. `/api/stats` HTTP 500 fix

Added a recursive `json_safe()` helper and wrapped the return value of `get_stats()` at its exit point (file `overlay/pymc_repeater/repeater/main.py`). Any `bytes`/`bytearray` at any nesting level is converted to a hex string, guaranteeing the payload is JSON-serializable regardless of source. A defensive `_json_safe()` was also added to `bridge_engine.py` for the `rules` field.

### 3. Invalid Packets tab moved

In `overlay/pymc_repeater/repeater/web/html/wm1303.html`, the **Invalid Packets** tab was reordered to sit between **Neighbours** and **Logs**.

### 4. Friendly channel names in forensics

Invalid-packet records now store a readable channel identifier: the configured channel name when frequency + spreading factor match a configured channel (e.g. `channel_e`), otherwise an informative fallback string such as `869.462MHz SF8BW125`. This makes it immediately clear whether traffic arrives on a configured channel or on foreign/non-standard settings (SF6/SF8/SF11 observed on the Channel-A frequency).

## Files changed

- `overlay/pymc_core/src/pymc_core/hardware/wm1303_backend.py` (Layer-2 validator integration + friendly channel names)
- `overlay/pymc_repeater/repeater/main.py` (`json_safe()` + wrapped `get_stats()`)
- `overlay/pymc_repeater/repeater/bridge_engine.py` (defensive `_json_safe()` on rules)
- `overlay/pymc_repeater/repeater/web/html/wm1303.html` (Invalid Packets tab reorder)
- `VERSION` (2.5.9 -> 2.5.10)

## Verification

1. `GET /api/stats` returns HTTP 200 (was 500); no `bytes is not JSON serializable` errors over sustained polling.
2. Live RX packets flow through `_dispatch_rx` and are validated; malformed packets are dropped before all consumers.
3. The Invalid Packets tab shows drops with reason, route type, hop count, channel/frequency, offender fingerprint, RSSI/SNR and raw hex.
4. Channel column shows readable names (`channel_e`, `869.462MHz SF8BW125`) instead of a placeholder.
5. Confirmed on both test repeaters: hundreds of malformed packets filtered (predominantly `length_implausible` and reserved `hash_size=4`), zero regressions in service operation.

## Upgrade

Run `./upgrade.sh` on the repeater. `upgrade.sh` already copies `wm1303_backend.py`, `main.py`, `bridge_engine.py` and `wm1303.html` from the overlay, so the fix is applied automatically. No database migration required (the `invalid_packets` table is created lazily; forensics retained 8 days).

## Known issues

- The `invalid_packets_stats` endpoint rejects a `limit` query parameter (returns 404 when one is sent); the UI does not send it, so there is no functional impact.
