# Release Notes — v2.7.1

**Release date:** 2026-07-22
**Type:** Patch release — critical upgrade fix (companion models sync)

---

## Overview

This patch fixes a critical regression that prevented the service from starting after upgrading an older installation to v2.7.0. Upgrades from pre-2.7 versions could leave the `openhop-repeater` service in a crash-restart loop.

Anyone on v2.7.0, or upgrading from an earlier release, should apply this patch.

---

## The problem

On startup the service failed with:

```
ImportError: cannot import name 'ChannelDataEvent'
from 'openhop_core.companion.models'
```

### Root cause

The WM1303 overlay ships a customised `openhop_core/companion/models.py` (it adds RSSI/SNR fields to the `Contact` model). The install/upgrade scripts copy that file over the installed `openhop_core` package.

The overlay copy had drifted behind the upstream `openhop_core` package: the installed package version pulls in a newer companion layer (`base_events.py`) that imports `ChannelDataEvent`, `ChannelMessageEvent`, `MessageEvent`, and `QueuedMessage` from `models.py`. The older overlay `models.py` did not define those symbols, so once it was copied over the package the import chain broke and the service could not start.

This did not surface on installations that were already up to date, only on upgrades where the newer companion layer met the older overlay `models.py`.

---

## The fix

The overlay `overlay/pymc_core/src/openhop_core/companion/models.py` was rebuilt on top of the current upstream companion `models.py`, then the WM1303-specific customisation was re-applied on top:

- **New base:** the current upstream `models.py`, which defines the full set of symbols the companion layer imports (`Channel`, `ChannelDataEvent`, `ChannelMessageEvent`, `Contact`, `MessageEvent`, `QueuedMessage`).
- **WM1303 customisation re-applied:** the `Contact` dataclass keeps its two extra fields `last_rssi` (dBm) and `last_snr` (dB), including the extraction of those values in `Contact.from_dict` and their inclusion in the constructed object.

The result is verified to contain both the full upstream symbol set (so the import chain resolves) and the WM1303 RSSI/SNR extension.

---

## Verification

- The rebuilt overlay `models.py` compiles cleanly and defines every symbol the companion layer imports.
- A diff against the upstream base shows only the RSSI/SNR additions — no other upstream content was lost.
- Deployed to a test device that was previously stuck in the crash-loop: the service returns to `active (running)` with zero restarts, no import errors in the log, RX/TX and channel bridges operate normally, and the v2.7.0 API endpoints (`/api/validate_config`, `/api/broker_presets`) return valid JSON.

---

## Upgrade

Run the standard upgrade command from the project root. No configuration changes are required; existing `config.yaml` and `wm1303_ui.json` are preserved.

---

## Compatibility

- SenseCAP M1 / Raspberry Pi OS (Bookworm and Trixie).
- WM1303 HAT (SX1302 + SX1261).
- Existing v2.6.x and v2.7.0 installations upgrade in place.
