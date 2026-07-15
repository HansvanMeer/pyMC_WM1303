# Release Notes — v2.6.1

**Release date:** 2026-07-15
**Status:** Hotfix release for the v2.6.0 OpenHop upstream migration

> ⚠️ **Recommended for all v2.6.0 installations.** v2.6.0 shipped the upstream `openhop_repeater@dev` rename, but a required CherryPy tool-registration side-effect was lost in that migration. Every endpoint using `tools.require_auth.on` (e.g. `/api/wm1303/*`, `/api/stats`, `/api/signal_history`) returned HTTP 500 until this fix. v2.6.1 restores full HTTP API functionality and completes the legacy → OpenHop filesystem move that v2.6.0 started.

---

## Summary

v2.6.1 is a small but important follow-up to the v2.6.0 OpenHop upstream migration. It:

1. **Restores the CherryPy `require_auth` tool registration** that was implicitly relied on in pre-OpenHop `pymc_repeater` but is no longer registered automatically in the upstream `openhop_repeater@dev` package.
2. **Completes the filesystem migration** by physically moving legacy `/var/log/pymc_repeater` and `/var/lib/pymc_repeater` content into the new `/var/log/openhop_repeater` and `/var/lib/openhop_repeater` locations — matching the `/etc/pymc_repeater` → `/etc/openhop_repeater` handling introduced in v2.6.0.

No functional changes elsewhere.

---

## Highlights

### 1. HTTP API Hotfix — Explicit `require_auth` Tool Registration

In v2.6.0, all endpoints that guard themselves with `tools.require_auth.on = True` began returning HTTP 500 with `AttributeError: 'Toolbox' object has no attribute 'require_auth'`. Root cause: the upstream `openhop_repeater@dev` package refactor removed the module-level import side-effect that previously registered the tool.

**Fix:** the WM1303 HTTP server now explicitly calls `register_require_auth_tool()` at server-start time, before any protected endpoint is mounted. This is a two-line change in `overlay/pymc_repeater/repeater/web/http_server.py`:

- **Import** (top of file): `from .auth.cherrypy_tool import register_require_auth_tool`
- **Registration** (server startup): `register_require_auth_tool()` invoked once, guarded by a clear inline comment tagging it as the WM1303 hotfix.

All previously-broken endpoints return HTTP 200 with valid JSON again:

| Endpoint | v2.6.0 | v2.6.1 |
|----------|--------|--------|
| `/api/wm1303/status` | 500 | 200 ✓ |
| `/api/stats` | 500 | 200 ✓ |
| `/api/signal_history` | 500 | 200 ✓ |
| `/api/wm1303/invalid_packets_recent` | 500 | 200 ✓ |

### 2. Physical `/var` Tree Migration

v2.6.0 renamed:

- `/var/log/pymc_repeater` → `/var/log/openhop_repeater`
- `/var/lib/pymc_repeater` → `/var/lib/openhop_repeater`

…in the config variables but did **not** migrate any legacy content on disk, so upgraded devices ended up with orphan legacy directories and lost their historic logs and SQLite databases (`repeater.db`, `spectrum_history.db`).

v2.6.1 adds a **physical migration** to `install.sh`, `upgrade.sh`, and `bootstrap.sh`:

- **If the new directory is empty** → `rsync -a --remove-source-files` performs a same-filesystem physical move. Historic logs and databases (potentially several MB) are preserved in the new location **without** duplicating them on disk. Empty legacy directories are cleaned up afterwards with `find -depth -type d -empty -delete`.
- **If the new directory already has content** → falls back to `cp -an` (archive mode, never-overwrite). Nothing is destroyed; both dirs remain intact so a rollback is always possible.
- Runs **before** the pre-upgrade backup step in `upgrade.sh` so the backup captures the migrated state.
- Runs in `bootstrap.sh` as a defense-in-depth measure, guarded by a `command -v rsync` check that silently defers to `install.sh` if `rsync` is not yet installed.

Both mechanisms follow the same conservative pattern as the existing `/etc/pymc_repeater` → `/etc/openhop_repeater` migration and are safe to re-run on already-migrated devices (idempotent).

---

## Bug Fixes

| # | Description | Severity | Fix |
|---|-------------|----------|-----|
| 1 | v2.6.0: all `require_auth`-guarded endpoints return HTTP 500 (`AttributeError` on CherryPy `Toolbox`) | **Critical** | Explicit `register_require_auth_tool()` call at server startup |
| 2 | v2.6.0: legacy `/var/log/pymc_repeater` and `/var/lib/pymc_repeater` not migrated to new OpenHop paths — historic logs and SQLite databases orphaned | High | `rsync --remove-source-files` physical move in install/upgrade/bootstrap |

---

## Files Changed

| File | Changes |
|------|---------|
| `VERSION` | `2.6.0` → `2.6.1` |
| `overlay/pymc_repeater/repeater/web/http_server.py` | Import + explicit `register_require_auth_tool()` call at server startup |
| `install.sh` | Physical migration of legacy `/var/log` and `/var/lib` directories, mirroring the existing `/etc` migration pattern |
| `upgrade.sh` | Same migration, placed before the pre-upgrade backup step so the backup captures the migrated state |
| `bootstrap.sh` | Defense-in-depth migration (guarded by `command -v rsync`), runs before install/upgrade to catch stale downstream scripts |
| `release_notes/RELEASE_NOTES_v2.6.1.md` | This file |

---

## Upgrade Instructions

### Standard upgrade

```bash
curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
```

### After upgrade

- **Hard refresh** your browser when opening the WM1303 Manager or pyMC Console UI:
  - **`Ctrl + Shift + R`** (Windows/Linux)
  - **`Cmd + Shift + R`** (macOS)
- Verify the version in the WM1303 Manager header shows **v2.6.1**.
- Quick sanity check that the API is healthy:
  ```bash
  curl -s http://127.0.0.1:8000/api/stats            | head -c 120; echo
  curl -s http://127.0.0.1:8000/api/signal_history   | head -c 120; echo
  ```
  Both should return valid JSON (HTTP 200), not an error page.
- Historic logs from `/var/log/pymc_repeater` and databases from `/var/lib/pymc_repeater` are automatically moved into the new OpenHop locations. If the new location already had content, the legacy directory is preserved as-is for rollback and can be removed manually once you have confirmed the upgrade is stable.

---

## Compatibility & Rollback

- **Compatible with all v2.6.0 installations.** No config-file changes required; the JWT identity, `wm1303_ui.json`, and channel configuration are untouched.
- Upgrades from v2.5.x and earlier: the v2.6.0 rename chain (config dir + service unit) is exercised end-to-end by v2.6.1 as well.
- Rollback is possible in-place by reverting to the v2.6.0 tag — the physical `/var` migration leaves the new dirs intact and (when the new dir already had content) the legacy dirs untouched.
