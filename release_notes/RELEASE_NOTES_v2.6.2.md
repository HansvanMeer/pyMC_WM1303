# Release Notes — v2.6.2

**Release date:** 2026-07-16
**Status:** Hotfix release for the WM1303 Manager UI version display

> ⚠️ **Recommended for all v2.6.0 / v2.6.1 installations that upgraded from v2.5.x.** After the v2.6.0 OpenHop rename, the WM1303 Manager UI header kept displaying the pre-upgrade v2.5.x version instead of the new v2.6.x version. Root cause: several code paths still read the legacy `/etc/pymc_repeater/version` file, which was never updated after the config-dir rename to `/etc/openhop_repeater/`. v2.6.2 synchronises the version file to both locations so the UI reports the correct version, without touching the still-hardcoded read paths (that full refactor is planned for v2.7).

---

## Summary

v2.6.2 is a small, surgical follow-up to the v2.6.0 / v2.6.1 OpenHop migration. It:

1. **Fixes the WM1303 Manager UI version display** by mirroring the `VERSION` file into the legacy `/etc/pymc_repeater/version` path in addition to the canonical `/etc/openhop_repeater/version`.
2. **Adds a clear roadmap comment** in both `install.sh` and `upgrade.sh` pointing to the full path refactor planned for v2.7.

No runtime code changes — this is a scripts-only release.

---

## Root Cause

A broader audit of the `overlay/` tree performed for this release found **68 hardcoded `/etc/pymc_repeater/...` paths spread across 12 Python files** (`wm1303_backend.py`, `wm1303_api.py`, `main.py`, `config.py`, `bridge_engine.py`, `channel_e_bridge.py`, `channel_f_bridge.py`, `debug_collector.py`, `api_endpoints.py`, `mesh_cli.py`, `sx1261_driver.py`, `engine.py`) with **zero references to `/etc/openhop_repeater/` anywhere in the code**.

The v2.6.0 "OpenHop migration" therefore renamed:

- the config directory (`/etc/pymc_repeater/` → `/etc/openhop_repeater/`)
- the log directory (`/var/log/pymc_repeater/` → `/var/log/openhop_repeater/`)
- the data directory (`/var/lib/pymc_repeater/` → `/var/lib/openhop_repeater/`)
- the systemd unit (`pymc-repeater.service` → `openhop-repeater.service`)

…but **did not touch the Python source**. All UI reads/writes still go to the legacy `/etc/pymc_repeater/` tree (which was safe-copied in v2.6.0, then diverged from the OpenHop copy as the UI updated its files and the install/upgrade scripts wrote the new version only to the OpenHop copy).

The UI version field is read from **`/etc/pymc_repeater/version`** (via `wm1303_api.py` line 1154). That file kept the pre-upgrade `2.5.x` content, while `/etc/openhop_repeater/version` was correctly updated to `2.6.0` / `2.6.1`.

---

## Fix (v2.6.2)

### Dual-write pattern in `install.sh` and `upgrade.sh`

Both scripts now write the `VERSION` file to **both** locations whenever the version is deployed:

```bash
cp "${SCRIPT_DIR}/VERSION" "${CONFIG_DIR}/version"          # /etc/openhop_repeater/version
if [ -d "${LEGACY_CONFIG_DIR}" ] && [ "${LEGACY_CONFIG_DIR}" != "${CONFIG_DIR}" ]; then
    cp "${SCRIPT_DIR}/VERSION" "${LEGACY_CONFIG_DIR}/version"  # /etc/pymc_repeater/version
fi
```

The legacy write is guarded by an existence check, so:

- **Existing installations** (both dirs present): version file is kept in sync.
- **Truly fresh installs** (no legacy dir): the legacy write is skipped; only the OpenHop path is used.

No other changes are needed for v2.6.2 — the UI displays whatever `wm1303_api.py` reads from `/etc/pymc_repeater/version`, and this now always matches the deployed version.

---

## Roadmap — v2.7 (full path refactor)

v2.6.2 is a targeted fix, **not** a completion of the OpenHop migration. The following is planned for v2.7:

- Introduce a central `openhop_core.paths` / `repeater.paths` module with a `resolve_config_path(name)` helper that prefers `/etc/openhop_repeater/<name>` and falls back to `/etc/pymc_repeater/<name>`.
- Replace all 68 hardcoded `/etc/pymc_repeater/...` references in the overlay with calls to that helper.
- Rename the installed Python package on disk from `pymc_repeater` to `openhop_repeater` so `import openhop_repeater` succeeds without conditional imports.
- Delete the dual-write shim added by v2.6.2 once the code no longer reads the legacy path.
- Optionally: replace `/etc/pymc_repeater/` with a symlink to `/etc/openhop_repeater/` on legacy devices as a one-shot cleanup, once the code is confirmed to read only the new path.

Until v2.7 lands, the dual-write pattern keeps the UI honest without touching any Python source.

---

## Bug Fixes

| # | Description | Severity | Fix |
|---|-------------|----------|-----|
| 1 | WM1303 Manager UI displays stale pre-upgrade version (2.5.x) after upgrading to v2.6.0 / v2.6.1 | High | Dual-write `VERSION` to `/etc/openhop_repeater/version` **and** `/etc/pymc_repeater/version` in `install.sh` and `upgrade.sh` |

---

## Files Changed

| File | Changes |
|------|---------|
| `VERSION` | `2.6.1` → `2.6.2` |
| `install.sh` | Dual-write of the `VERSION` file to `${LEGACY_CONFIG_DIR}/version` alongside `${CONFIG_DIR}/version`, guarded by an existence check |
| `upgrade.sh` | Same dual-write, applied inside the existing `Updating version file` step |
| `release_notes/RELEASE_NOTES_v2.6.2.md` | This file |

No Python code changes.

---

## Upgrade Instructions

### Standard upgrade

```bash
curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
```

### After upgrade

- **Hard refresh** your browser when opening the WM1303 Manager UI:
  - **`Ctrl + Shift + R`** (Windows/Linux)
  - **`Cmd + Shift + R`** (macOS)
- Verify the version in the WM1303 Manager header shows **v2.6.2**.
- Optional sanity check on the device:
  ```bash
  cat /etc/openhop_repeater/version   # should be 2.6.2
  cat /etc/pymc_repeater/version      # should also be 2.6.2 (only on devices upgraded from v2.5.x)
  ```

---

## Compatibility & Rollback

- **Compatible with all v2.6.0 and v2.6.1 installations.** No config, JWT, or database changes.
- **Compatible with truly fresh installs** (no legacy `/etc/pymc_repeater/` dir): the legacy write is skipped by the existence guard.
- Rollback: revert to the v2.6.1 tag. The legacy `version` file will drift back to the older content on the next version bump, but no runtime behaviour changes.
