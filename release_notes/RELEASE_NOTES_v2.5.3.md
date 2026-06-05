# Release Notes — v2.5.3

**Release date:** 2026-06-03  
**Type:** Hotfix — restores SX1302 RX and on-air MeshCore compatibility on all WM1303 installs  
**Upgrade path:** `curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash`

---

## Summary

This hotfix resolves two critical regressions in v2.5.2 that, in combination, made fresh SenseCAP M1 installs unable to receive on `chan_multiSF` and invisible to other MeshCore devices on-air. Both fixes are confirmed working on three independent SenseCAP M1 units and on the project's reference repeater.

It also fixes a long-standing usability bug from GitHub issue #7 (Bug E) where the WM1303 UI "LBT (dBm)" control was cosmetic only — the value the user set was written into `lbt_rssi_target` but `lora_pkt_fwd` reads `lbt_threshold` for actual TX-blocking, and the two fields silently diverged. Both fields are now kept in sync on every channel save.

Finally, this release closes two long-standing companion-app limitations that have been on the roadmap since v2.5.2: the repeater's own identity now appears as a discovered node in the companion app's contact list (self-ADVERT injection), and the repeater's outbound packets now appear in the companion's live activity log as "heard repeats" (self-TX loopback). Both are implemented in-process with sentinel signal metrics and zero impact on RX availability or TX latency.

Users running v2.5.2 are strongly advised to upgrade. No configuration changes are required — the fixes apply automatically on next service start.

---

## Changes

### Bug Fixes

#### HAL — ARB MCU dual-demodulation correlators force-enabled (#12, #11)

In `sx1302_arb_start()` (`libloragw/src/loragw_sx1302.c`), the WM1303-specific branch — always taken because `wm1303_backend.py` hardcodes `precision_timestamp.enable = False` — wrote `0x00` to ARB register 3. The accompanying source comment claimed "FORCE ENABLED for all SF", but per the SX1302 datasheet, register 3 controls per-SF dual-demodulation correlator enable (1 bit per SF, LSB=SF5 .. MSB=SF12, `0:Disable 1:Enable`). The value `0x00` disables every SF correlator, leaving `chan_multiSF` deaf.

The write is now `0xFF`, matching the comment's intent and enabling dual-demodulation correlators for all spreading factors. On affected SenseCAP M1 units this immediately restores `rxpk` delivery on `chan_multiSF` with both dual demodulators active.

**Why this was silent in earlier versions.** In the v2.4.7 era the project defaulted `precision_timestamp.enable = True`, so the WM1303 branch was never taken and the `0x00` was never reached. The value was always wrong; it just was not exercised. Hardcoding `precision_timestamp.enable = False` in a later refactor activated the dormant bug for every WM1303 install.

#### Backend — `lorawan_public` no longer derived from MeshCore sync_word (#12, #11)

In `_generate_bridge_conf()` (`overlay/pymc_core/.../wm1303_backend.py`), the board-level `lorawan_public` flag was derived from the device-wide `sync_word` setting. This conflated two unrelated namespaces:

- **LoRa PHY sync word**: the on-air byte the SX1302 ARB correlators match against. MeshCore (and Meshtastic and any non-LoRaWAN LoRa network) uses `0x12` — the value the LoRaWAN spec labels "private".
- **MeshCore protocol-level network identifier**: a 16-bit value (`0x1424` "Private" or `0x3444` "Public") that MeshCore software uses to separate communities. It has no relationship to the LoRaWAN public/private sync word.

A MeshCore "Public" install (`sync_word=0x3444`) was being mapped to `lorawan_public=True`, which programmed the SX1302 board with the LoRaWAN public on-air sync word `0x34`. Receiving MeshCore peers listen on `0x12`, so their ARB correlators silently rejected every packet before demodulation — the transmitting unit appeared healthy locally but was invisible to all other MeshCore devices.

`_lorawan_public` is now hard-coded to `False`. The on-air LoRa PHY sync word is therefore always `0x12`, which is correct for MeshCore and all other non-LoRaWAN LoRa networks. The UI sync_word picker continues to work and is still written into `bridge_conf.json` for MeshCore protocol-level identification — it just no longer affects the SX1302 board configuration.

The header comment block in the same function was rewritten to remove the LoRaWAN/MeshCore conflation and to document the new design intent explicitly.

#### UI — LBT threshold control now functional (#7 Bug E)

The WM1303 UI's per-channel "LBT (dBm)" control writes `lbt_rssi_target` into `wm1303_ui.json`, but `lora_pkt_fwd` reads `lbt_threshold` for the actual TX-blocking decision. The two fields had silently diverged for a long time because bootstrap initialised `lbt_threshold = -80` directly, and the pre-existing `setdefault` sync in `_channels_get()` only ran on read — a no-op once both fields existed. The result: UI changes to LBT were cosmetic. They appeared to save (and the UI displayed the new value on reload), but the HAL kept using the stale bootstrap value, so adverts and other TX continued to be blocked or allowed exactly as before.

The fix adds an explicit sync block to `_channels_post()` in `overlay/pymc_repeater/repeater/web/wm1303_api.py`, right after the defensive `sync_word` strip and before `_save_ui(ui)`. On every channel save, for each channel dict:

- if `lbt_rssi_target` is present (the UI source-of-truth), its value is copied into `lbt_threshold`;
- otherwise, if only `lbt_threshold` is present (legacy callers), its value is mirrored back into `lbt_rssi_target`;
- if neither is set, the channel is left untouched.

The save handler now guarantees the two fields hold the same value after every UI save, so HAL decisions match the UI display. Verified with a 4-case unit test (rssi-only, threshold-only, both-present preferring `lbt_rssi_target`, and neither): all OK.

### Feature additions

#### Companion visibility — self-ADVERT injection for node discovery

RF-received ADVERTs were already routed to companion bridges by `packet_router._dispatch_received` (so neighbours' ADVERTs populated the companion's contact list), but the repeater's own outbound ADVERT only went through `bridge_engine.inject_packet('repeater', raw_bytes)` for RF TX — never to companion bridges. As a result, the companion app never saw the repeater's own identity as a discovered node, and users could not see their own repeater in the contact list.

`send_advert()` in `overlay/pymc_repeater/repeater/main.py` now feeds the freshly built ADVERT packet through each registered `self.companion_bridges[*].process_received_packet(packet)` directly after the bridge-engine injection and before `repeater_handler.mark_seen(packet)`. Sentinel `rssi=0` / `snr=0.0` are set on the packet first because it did not arrive via RF and no real values exist; companion bridges treat these as "local". A summary log line `Self-advert delivered to N companion bridge(s) for node discovery` confirms delivery. Placement matters: this MUST run before `mark_seen`, which suppresses RF-echo re-injection by the BridgeEngine but does not gate companion delivery. Per-bridge try/except prevents one misbehaving companion from blocking the others.

Verified on the reference repeater by triggering `POST /api/send_advert` (JWT-authenticated): the log shows `Advert injected into bridge engine as repeater source` → `Self-advert delivered to 1 companion bridge(s) for node discovery` → `Sent flood advert 'pyRpt6400'` in the expected order.

#### Companion visibility — self-TX loopback for heard-repeat activity

The WM1303 cannot hear its own RF transmission, so companion apps never saw the repeater's outbound packets in their live activity log alongside received traffic. This includes both the repeater's own ADVERTs and every neighbour packet it forwarded — none of it was visible to the user.

`_forward_by_rules()` in `overlay/pymc_repeater/repeater/bridge_engine.py` now calls `self._fire_raw_rx_callbacks(data, 'self_tx:<channel>', 0, 0.0)` directly after `_store_tx_echo_hash(data)` and `_record_dedup_event('forwarded', ...)`, inside the `rf_sends` for-loop. The loopback runs once per successful TX target and covers BOTH `kind='radio'` (chan_multiSF A–D) and `kind='endpoint'` (channel_e SX1261) because both code paths converge on the same post-try block (the except branch above uses `continue`, skipping this code on TX failure). The result is a 0x88 frame pushed to every registered companion frame server — exactly the same shape as if a neighbour had repeated the packet — with a `self_tx:<channel>` source name so companion-side filtering can distinguish loopback frames from real RF-received frames, and with sentinel `rssi=0` / `snr=0.0` to prevent garbage in the signal-metrics display.

Three design principles are satisfied: (1) **RX availability #1** — pure in-memory callback dispatch, no radio operation, zero SX1302/SX1261 state change. (2) **TX ASAP** — runs strictly after TX completes, so it cannot delay TX. (3) **No double-delivery** — `_store_tx_echo_hash(data)` above already armed the engine's TX-echo filter, so if the same packet returns via real RF (neighbour repeat), the engine drops it as `TX_ECHO` before re-forwarding, and the companion sees only one copy.

Verified on the reference repeater: `journalctl` shows `BridgeEngine raw RX → 1 companion server(s) (<N> bytes, src=self_tx:channel_e, rssi=0, snr=0.0)` after every TX, including the forced 120-byte FLOOD/ADVERT, interleaved with normal `src=channel_e` rows that carry real RF rssi/snr values from neighbour traffic.

### Diagnostics additions

#### Cache-stats endpoint for state-accumulation monitoring

Long-running deployments can accumulate state in the various in-memory dedup and config caches, and until now there was no way to inspect that state without attaching a debugger. A new read-only endpoint `GET /api/wm1303/cache_stats` (in `overlay/pymc_repeater/repeater/web/wm1303_api.py`) returns a JSON snapshot of every in-memory cache with its `size`, `max_size`, `ttl_seconds`, `oldest_age_seconds`, `newest_age_seconds`, a `ts_mode` flag (`wall` / `monotonic` / `n/a`), and a human-readable description, plus engine counters.

Covered caches: BridgeEngine `_seen` (packet-hash dedup, TTL 300 s), `_tx_echo_hashes` (RF-echo detection, TTL 0.5 s), `_dedup_events` ring buffer (deque, maxlen 500); WM1303Backend `_tx_echo_hashes` (TTL 30 s), `_rx_dedup_cache` (multi-demod dedup), `_tx_ack_cache` (OrderedDict, max 512, TTL 30 s), `_lbt_config_cache` and `_cad_config_cache` (per-channel, TTL 5 s), and the Channel E / Channel F config snapshot caches. Counters include `forwarded_packets`, `dropped_duplicate`, `dropped_filtered`, `fwd_echo_detected`, `tx_echo_detected`, and `tx_unknown_echo_detected`.

All ages are computed against a single reference clock sampled at the start of the handler (both `time.monotonic()` and `time.time()` are captured up front), so reported ages are internally consistent. The endpoint is purely read-only — it iterates existing dicts and never mutates state — so it has zero impact on RX availability or TX latency. Operators can poll it to confirm that dedup and TX-echo caches stay within their TTL bounds and that config caches are being refreshed on schedule, which is the primary diagnostic signal for the long-uptime state-accumulation issue tracked in the TODO (#24/#25).

Verified on the reference repeater (JWT-authenticated) after sustained traffic: the `seen_dedup` cache reported `oldest_age_seconds=26.0`, `newest_age_seconds=2.55`, `ttl_seconds=300`, `ts_mode=monotonic`, with all other caches populated as expected.

---

## Files Changed

| File | Change |
|------|--------|
| `overlay/hal/libloragw/src/loragw_sx1302.c` | `sx1302_arb_start()` WM1303 branch: `sx1302_arb_debug_write(3, 0x00)` → `sx1302_arb_debug_write(3, 0xFF)` with an explanatory inline comment cross-referencing GitHub issue #12 |
| `overlay/pymc_core/src/pymc_core/hardware/wm1303_backend.py` | `_generate_bridge_conf()`: `_lorawan_public` hard-coded to `False` with a ~20-line block comment explaining the MeshCore-vs-LoRaWAN sync word semantics; header comment block updated to remove obsolete LoRaWAN-centric framing |
| `overlay/pymc_repeater/repeater/web/wm1303_api.py` | `_channels_post()`: explicit per-channel `lbt_rssi_target` / `lbt_threshold` sync block added after the defensive `sync_word` strip; treats `lbt_rssi_target` as the UI source-of-truth and copies the value into `lbt_threshold` on every save (Bug E fix from issue #7). Also adds the read-only `GET /api/wm1303/cache_stats` diagnostics endpoint (route dispatch + `_cache_stats_get` handler) returning a snapshot of all in-memory dedup/echo/config caches with sizes, TTLs and entry ages |
| `overlay/pymc_repeater/repeater/main.py` | `send_advert()`: per-bridge `process_received_packet(packet)` loop added between `bridge_engine.inject_packet(...)` and `repeater_handler.mark_seen(packet)`, with sentinel `rssi=0` / `snr=0.0` so the repeater's own identity is registered in every companion's contact list on node discovery |
| `overlay/pymc_repeater/repeater/bridge_engine.py` | `_forward_by_rules()`: `self._fire_raw_rx_callbacks(data, 'self_tx:<channel>', 0, 0.0)` added after `_store_tx_echo_hash(data)` / `_record_dedup_event('forwarded', ...)` inside the `rf_sends` for-loop, so every successful TX (radio A–D and SX1261 endpoint) appears in the companion's live activity log as a 0x88 heard-repeat frame |

No configuration files, no installer changes, no UI changes. Upgrading requires a HAL rebuild of `lora_pkt_fwd` (handled automatically by `upgrade.sh`) and a backend service restart.

---

## Upgrade Notes

- Existing v2.5.2 installs upgrade in place — no manual config changes required. The `lorawan_public` value in `bridge_conf.json` is regenerated from backend state on each service start, so the corrected value (`false`) appears automatically.
- Running `upgrade.sh` rebuilds `libloragw.a` and `lora_pkt_fwd` from the patched HAL source and installs the new binary into `~/wm1303_pf/`.
- Stuck units that entered the SX1302 RX-deaf state under v2.5.2 (see issue #11 comment 14) are not recovered by this hotfix alone; recovery investigation is tracked separately as a known limitation below.

---

## Related Issues

- #11 — SX1302 `rxnb=0` + TX malformed on three SenseCAP M1 units — root cause identified and confirmed in the wild
- #12 — Two v2.5.2 bugs: ARB MCU correlators disabled + `lorawan_public` derived incorrectly from `sync_word`
- #7 (Bug E) — UI "LBT (dBm)" control cosmetic only: `lbt_rssi_target` (UI) and `lbt_threshold` (HAL) diverged on every save

---

## Acknowledgements

Both root-cause analyses, the exact source-line locations, and the proposed fixes were contributed by **@fahimshariff-au**, who diagnosed the v2.5.2 regressions across three independent SenseCAP M1 units and validated the proposed patches before submitting them upstream. The hotfix in this release is a direct application of those findings. Thank you.

---

## Known Limitations

- **Stuck SX1302 RX chain after a v2.5.2 bad-IF startup attempt**: units that already entered the deaf state under v2.5.2 may not recover with this hotfix alone — GPIO reset sequence and SPI reinit are insufficient on those units, while KISS-wrapper builds (`libmeshcore_lgw.so`) successfully bring up the same physical SX1302. Recovery path inside `lgw_start()` is under investigation; tracked as a follow-up.

## Resolved from v2.5.2 "Known Limitations"

- **Self-visibility** (was v2.5.2 "Pi01 self-visibility"): resolved by the self-ADVERT injection added in this release. The repeater's own identity now appears as a discovered node in the companion app's contact list.
- **Self heard-repeat** (was v2.5.2 "Self heard-repeat"): resolved by the self-TX loopback added in this release. Every successful TX (the repeater's own ADVERT and every forwarded neighbour packet) now appears in the companion's live activity log as a heard-repeat 0x88 frame.
