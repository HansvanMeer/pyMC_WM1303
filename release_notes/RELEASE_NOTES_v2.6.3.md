# Release Notes — v2.6.3

**Release date:** 2026-07-16
**Status:** Cleanup release — removes the v2.6.2 dual-write shim now that the path refactor is in production

> ℹ️ **v2.6.3 is a small follow-up to v2.6.2.** The v2.6.2 refactor moved every `/etc/pymc_repeater/...` read/write in the overlay to the `openhop_core.paths.resolve_config_path()` helper. The dual-write of `VERSION` to `/etc/pymc_repeater/version` from `install.sh` / `upgrade.sh` was kept as a safety net for one release. v2.6.3 removes that shim: the canonical location `/etc/openhop_repeater/version` is now the single source of truth. Backwards compatibility for devices upgrading from v2.5.x is preserved via the existing Phase 1 legacy config-dir migration in `upgrade.sh` and via the `resolve_config_path()` helper's legacy-fallback branch.

---

## Summary

v2.6.3 is a scripts-only cleanup release. It:

1. **Removes the v2.6.2 dual-write shim** from `install.sh` and `upgrade.sh`. The `VERSION` file is now written only to `/etc/openhop_repeater/version` (the canonical OpenHop location).
2. **Preserves full backwards compatibility** for every supported upgrade path (v2.5.x → v2.6.3, v2.6.x → v2.6.3) via existing mechanisms.
3. **Adds explanatory NOTE comments** in both scripts where the shim used to sit, documenting why the dual-write is no longer required.

No Python code changes. No runtime behaviour changes on already-migrated devices.

---

## Why the shim can go

The v2.6.2 dual-write shim existed because 68 hardcoded `/etc/pymc_repeater/...` references in 12 overlay Python files still read the legacy path directly. That is no longer true: v2.6.2 replaced every one of those references with `openhop_core.paths.resolve_config_path(name)` calls (61 helper calls plus 4 subprocess shell commands that try both paths in shell).

`resolve_config_path()` prefers `/etc/openhop_repeater/<name>` and transparently falls back to `/etc/pymc_repeater/<name>` when only the legacy file exists. The helper therefore reads the correct file regardless of whether a device has been migrated to the OpenHop layout. Once the overlay stopped reading `/etc/pymc_repeater/version` directly, the dual-write became redundant.

---

## Fix (v2.6.3)

### `install.sh`

The dual-write block after `chown ${PI_USER}:${PI_USER} "${CONFIG_DIR}/version"` is removed. Only the canonical write remains:

```bash
cp "${SCRIPT_DIR}/VERSION" "${CONFIG_DIR}/version"     # /etc/openhop_repeater/version
chown ${PI_USER}:${PI_USER} "${CONFIG_DIR}/version"
# NOTE: The v2.6.2 dual-write shim to ${LEGACY_CONFIG_DIR}/version was removed in v2.6.3
# now that the overlay code reads via openhop_core.paths.resolve_config_path().
ok "v$(cat ${SCRIPT_DIR}/VERSION)"
```

### `upgrade.sh`

Same removal in the "Updating version file" step of Phase 8.

---

## Backwards compatibility matrix

| Upgrade path | Works? | Why |
|--------------|--------|-----|
| **Fresh install v2.6.3** | ✅ | `install.sh` creates `/etc/openhop_repeater/` and writes `VERSION` there. Overlay reads via `resolve_config_path()`, which returns the OpenHop path. No legacy dir needed. |
| **Upgrade v2.5.x → v2.6.3** | ✅ | Phase 1 legacy config-dir migration in `upgrade.sh` (`cp -an ${LEGACY_CONFIG_DIR}/. ${CONFIG_DIR}/`) safe-copies every legacy file — including the old `version` — into `/etc/openhop_repeater/` **before** the new VERSION is written. Nothing is lost. |
| **Upgrade v2.6.0 / v2.6.1 → v2.6.3** | ✅ | `/etc/openhop_repeater/` already exists on these devices. `upgrade.sh` overwrites the canonical `version` file. The stale `/etc/pymc_repeater/version` is ignored by the overlay code (which now reads via the helper). |
| **Upgrade v2.6.2 → v2.6.3** | ✅ | Straight version bump. Both dirs already in sync; only the canonical write happens now. |
| **Rollback v2.6.3 → v2.6.2** | ✅ | v2.6.2's dual-write shim runs again on next install/upgrade, re-syncing `/etc/pymc_repeater/version`. No data loss. |

### Legacy fallback still active

The `resolve_config_path()` helper keeps its legacy-fallback branch. If a device ever ends up in an inconsistent state where a config file only exists in `/etc/pymc_repeater/` (not `/etc/openhop_repeater/`), the overlay still finds it. This branch will only be removed once every field device has been rebuilt cleanly.

---

## Files Changed

| File | Changes |
|------|---------|
| `VERSION` | `2.6.2` → `2.6.3` |
| `install.sh` | Removed 8-line dual-write block after `chown ${CONFIG_DIR}/version`; replaced with 7-line explanatory NOTE comment. |
| `upgrade.sh` | Same removal in the "Updating version file" step. |
| `release_notes/RELEASE_NOTES_v2.6.3.md` | This file. |
| `TODO.md` | Item #204 (open) further updated — shim-removal is now done; only upstream package rename remains. Item #211 added to completed section. |

No Python code changes.

---

## Upgrade Instructions

### Standard upgrade (any v2.5.x / v2.6.x → v2.6.3)

```bash
cd ~/pyMC_WM1303
git pull
sudo ./upgrade.sh
```

### One-liner bootstrap (fresh SenseCap M1 install or full rebuild)

```bash
curl -fsSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
```

### After upgrade

Verify the WM1303 Manager UI shows **v2.6.3** in the header. Hard-refresh the browser (`Ctrl + Shift + R` on Windows/Linux, `Cmd + Shift + R` on macOS) if the cached header still shows an older version.

Verify with the API:

```bash
curl -s http://<repeater-ip>:8000/api/wm1303/status | grep -o '"version":"[^"]*"'
# Expected: "version":"2.6.3"
```

---

## Compatibility & Rollback

Fully compatible with all v2.5.x and v2.6.x devices. Rollback is safe — see the compatibility matrix above.
