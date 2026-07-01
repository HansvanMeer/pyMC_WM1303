# Release Notes — v2.5.2

**Release date:** 2026-05-25  
**Type:** Minor release — TCP companion fixes, radio calibration, WiFi stability  
**Upgrade path:** `curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash`

---

## Summary

This release fixes critical TCP companion integration issues, corrects AU915 radio calibration, and adds WiFi power-save detection for wireless-connected devices.

---

## Changes

### Bug Fixes

#### TCP Companion — RF Deaf Fix (#5)
TCP companion nodes were unable to receive RF messages because the `BridgeEngine` bypasses the upstream `Dispatcher`. The `BridgeRepeaterHandler` now calls `router.enqueue()` to deliver parsed RF packets to companion bridges, restoring full message delivery.

#### TCP Companion — TRACE_RESP Handling (#5)
Incoming RF `TRACE_RESP` packets were misidentified as outgoing TRACE requests and dropped. The `PacketRouter` now distinguishes between locally-originated TRACE TX and RF-received TRACE_RESP, ensuring trace path responses reach the companion app.

#### TCP Companion — Stale Echo Filter (#5)
After renaming a companion node via the UI, the old name lingered in the echo-check filter, causing messages to be silently discarded. Node name synchronization now updates the echo filter immediately upon rename.

#### TCP Companion — Heard Repeats / Raw RX Pre-filter (#8)
The raw RX push (0x88 frame) to companion apps was placed after echo/dedup filtering in the `BridgeEngine`, which meant repeated packets (heard repeats) were dropped before reaching the companion. The raw RX callback is now registered in `WM1303Backend` — the earliest possible point — so companions receive **all** RF packets before any filtering.

#### TCP Companion — ADVERT Routing & Node Discovery (#5, #8)
ADVERT packets from RF were not delivered to companion bridges, preventing node discovery. The `PacketRouter` now routes ADVERTs to all registered companion bridges. Combined with a preference fix (`manual_add_contacts` forced to `0`), companion apps now show discovered nodes automatically.

#### TCP Companion — Bridge-Aware TX Injector
Messages sent from the companion app were injected via the `PacketRouter`/`Dispatcher`, which has no active radio on WM1303 hardware. A new `_companion_injector` method routes companion-originated TX through the `BridgeEngine` for RF transmission on all active channels, while also enqueuing the packet for companion self-delivery.

#### TCP Companion — _handlers Init Hardening (#5)
An `AttributeError` on `RepeaterCompanionBridge.__init__` was fixed by adding try/except guards around `_handlers` access during early initialization.

#### AU915 SX1261 Calibration Bytes (#7, #10)
The AU915 region used incorrect SX1261 calibration bytes `(0xE5, 0xE9)` instead of the correct `(0xE1, 0xE9)`, restricting the frequency window to 916–932 MHz instead of the full 900–932 MHz range.

#### VirtualLoRaRadio Instance Attributes (#10)
The `VirtualLoRaRadio` class stored radio parameters only in `channel_config`, causing `engine.py` to fall back to EU868 defaults via `getattr()`. Per-channel parameters (spreading_factor, coding_rate, tx_power, etc.) are now set as instance attributes.

### New Features

#### WiFi Power Save Detection & Disabling
WiFi power-save mode can cause SSH disconnects and network instability on wirelessly-connected devices. Both `install.sh` and `upgrade.sh` now:
- Detect WiFi interfaces automatically
- Check current power-save status via `iw`
- Disable power-save immediately if enabled
- Apply a persistent fix using the best available method:
  - **NetworkManager** — `/etc/NetworkManager/conf.d/99-wifi-powersave-off.conf`
  - **dhcpcd** — `/etc/dhcpcd.exit-hook` (Raspberry Pi OS)
  - **udev rule** — `/etc/udev/rules.d/70-wifi-powersave.rules` (generic fallback)

---

## Files Changed

| File | Change |
|------|--------|
| `overlay/pymc_repeater/repeater/main.py` | `_companion_injector`, `_bridge_repeater_handler` updates, companion bridge wiring |
| `overlay/pymc_repeater/repeater/packet_router.py` | ADVERT/TRACE_RESP routing to companion bridges |
| `overlay/pymc_repeater/repeater/companion/bridge.py` | Force `manual_add_contacts=0`, `set_our_node_name` hardening, echo filter sync |
| `overlay/pymc_repeater/repeater/bridge_engine.py` | Raw RX callback registration (`register_on_raw_rx`, `_fire_raw_rx_callbacks`) |
| `overlay/pymc_core/src/pymc_core/hardware/wm1303_backend.py` | Pre-filter raw RX callback for companion delivery |
| `overlay/pymc_core/src/pymc_core/hardware/region_config.py` | AU915 SX1261 calibration bytes correction |
| `overlay/pymc_core/src/pymc_core/hardware/virtual_radio.py` | Instance attributes for per-channel radio parameters |
| `install.sh` | WiFi power-save detection & persistent disabling |
| `upgrade.sh` | WiFi power-save detection & persistent disabling |

---

## Related Issues

- #5 — TCP companion RF deaf, TRACE_RESP, echo filter, ADVERT routing
- #7 — AU915 SX1261 calibration bytes
- #8 — Heard Repeats / raw RX pre-filter
- #10 — VirtualLoRaRadio getattr defaults

---

## Known Limitations

- **the reference device self-visibility**: The repeater's own identity does not appear as a discovered node in the companion app (requires internal self-ADVERT injection — planned for future release).
- **Self heard-repeat**: The repeater cannot hear its own RF transmission, so self-heard-repeats are not shown (requires internal loopback — planned for future release).
