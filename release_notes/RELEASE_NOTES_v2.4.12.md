# Release Notes — v2.4.12

**Type:** Pre-release (test release)
**Date:** 2026-05-24
**Compatibility:** pyMC_core dev, pyMC_Repeater dev, HAL v2.10

> ⚠ This is a **test release**. Production use is at your own risk. Always back up `/etc/pymc_repeater/` before upgrading.

---

## Summary

v2.4.12 closes out the remaining issues from [#7](https://github.com/HansvanMeer/pyMC_WM1303/issues/7) (AU915 setup experience) and hardens the install and upgrade scripts based on edge cases observed during the v2.4.11 runtime upgrade to the latest pyMC_core/pyMC_Repeater dev branches.

No breaking changes. Drop-in upgrade from v2.4.11.

---

## Issue #7 — Final fixes

### Bug 1 — Non-interactive bootstrap defaults are now visible

The `curl … | sudo bash` install path has no TTY, so the wizard defaults to EU868 + EU-Default + Private sync word. Previously this happened with only quiet info-level messages, which made it easy to miss that the wrong region had been installed.

A prominent warning block is now printed at the end of the wizard when **all three** region/preset/sync-word values were defaulted (no `WM1303_REGION` / `WM1303_PRESET` / `WM1303_SYNC_WORD` env vars supplied). The block shows the defaults that were applied and the exact override command for next time.

If the user supplied at least one env var, the warning is suppressed — the install was clearly intentional.

### Bug 2 — Region change in the UI warns about channel frequencies

When a user changes the Region dropdown in the WM1303 manager UI to a value different from the currently saved region, a banner now appears in the Region & Regulatory card:

> **⚠ Region changed.** Channel frequencies are **not** automatically updated. Review the Channels tab and update each channel's frequency, bandwidth, and other settings to match the new region's plan. Click **Save** below to apply the region change.

The banner clears automatically when the region is saved, or when the user reverts the dropdown back to the original value. This prevents the silent failure mode described in issue #7 where a user corrected the region via UI but was left with stale EU868 channel frequencies and no indication that something needed attention.

### Bug 3 — Channel F IF range guard prevents pkt_fwd crash loops

If an upgraded install has Channel F enabled with a frequency that falls outside the SX1302 standard-channel IF range (more than ~730 kHz from the RF0 center for BW125, less for wider bandwidths), the HAL rejects the configuration with *"invalid configuration for Lora standard channel"* and `lora_pkt_fwd` enters a crash loop, blocking access to the web UI.

The backend now applies a runtime IF range check when generating `bridge_conf.json` for pkt_fwd. If Channel F's frequency is out of range relative to the auto-computed or manually-set RF0 center, Channel F is force-disabled with a clear warning in the service logs explaining what to fix. The service then starts cleanly and the user can correct Channel F's frequency (or `rf_center_freq_mhz`) via the UI without a crash loop blocking access.

This complements the pre-save IF range validation that already exists for UI-driven channel edits — that path catches the issue before save, this path catches the issue at service start for configs that pre-date the validation or were edited externally.

### Community notes from issue #7 — intentionally not pursued

- **AU sub-region presets (AU-Mid / AU-Narrow / AU-Wide)**: deferred to upstream pymc_repeater where the bridge engine is becoming a standard component. Tracking via upstream.
- **Standalone-vs-bridge mode wizard**: the standalone use case is already covered by activating one channel and creating two bridge rules. Adding a wizard branch would not simplify this enough to justify the extra UX complexity.

---

## Install and upgrade script hardening

Three defensive improvements applied to both `install.sh` and `upgrade.sh`, prompted by edge cases observed during the v2.4.11 dev-branch upgrade on the reference device:

### Robust `git` update flow

Replaced the `git checkout -- . && git clean && git pull` sequence with `git fetch + git checkout <branch> + git reset --hard origin/<branch> + git clean -fd`. Idempotent and eliminates *"Your local changes would be overwritten by merge"* errors that occur when the overlay-copy has modified tracked upstream files since the last run.

### Recursive overlay re-apply

The pymc_core overlay re-apply step (used when `pip install -e .` falls back to a non-editable install on Python 3.13) now uses `rsync -a` instead of single-level `cp *.py`. Sub-directories and non-.py files are no longer silently skipped. Same change applied to the companion and pymc_repeater re-apply blocks.

### WM1303Backend import sanity check

A new explicit `from pymc_core.hardware import WM1303Backend` test runs after the overlay re-apply step. If the import fails (e.g. because pip didn't propagate the overlay to site-packages), the script logs a clear warning so the cause is visible immediately, rather than letting the service start in degraded mode without explanation.

---

## Upgrade

Standard upgrade procedure from v2.4.11:

```
sudo /opt/pymc_repeater/repos/pyMC_WM1303/upgrade.sh
```

The new git-reset flow makes upgrade.sh tolerant of dirty overlay state from a previous run, so no manual cleanup is required even if the previous upgrade was interrupted.

Fresh installs via the bootstrap one-liner are unchanged. For non-interactive installs (e.g. provisioning scripts), the env-var form is now strongly recommended:

```
WM1303_REGION=AU915 WM1303_SYNC_WORD=public \
  curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh \
  | sudo -E bash
```

---

## Known issues

- **VirtualLoRaRadio EU868 fallback** (issue #10) — not addressed in this release. Tracked separately.
- **TCP companion inbound RX path** (issue #5) — the bridge engine does not yet call `CompanionBridge.received_group_message()` on RF-received packets. Architectural fix planned for a future release; the v2.4.11 `_response_injector` covers the related repeater-response path but not this one.
- **Heard Repeats CRC mismatch** (issue #8) — the WM1303 bridge repackages companion packets before RF transmission, so ACK CRCs don't match what the companion expects. Fix requires changes in pymc_core; tracking via the zindello fork branch.

---

## Compatibility

- pyMC_core: any dev-branch commit from 2026-05-22 onwards (release v1.0.12)
- pyMC_Repeater: any dev-branch commit from 2026-05-22 onwards
- HAL: v2.10
- Hardware: SenseCAP M1 (SX1302 + SX1261), Raspberry Pi 3/4/5
- OS: Raspberry Pi OS Lite 64-bit (Bookworm), Python 3.11+ (tested on 3.13)
