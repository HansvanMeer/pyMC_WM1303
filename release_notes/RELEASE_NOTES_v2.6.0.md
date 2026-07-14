# v2.6.0 — OpenHop upstream migration (openhop_core / openhop_repeater)

**Release date**: 2026-07-14
**Type**: Minor release with a **breaking change** in package identity, service name and config path.
**Upgrade path**: `curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash`

---

## Summary

Upstream `pyMC_core` / `pyMC_Repeater` were renamed to `openhop_core` / `openhop_repeater`. This release migrates the WM1303 overlay, install/upgrade/bootstrap scripts and systemd unit to the new OpenHop identity, and pins the forks to their `dev` branch. Existing installations upgrade in place: the legacy `pymc-repeater.service` is stopped and removed, the new `openhop-repeater.service` is installed and enabled, and `/etc/pymc_repeater` is migrated to `/etc/openhop_repeater` with the JWT identity, `version` and `wm1303_ui.json` preserved. No mesh state, database, or web configuration is lost.

## Why

The upstream fork chain (`rightup/pyMC_core` → `HansvanMeer/pyMC_core`, `rightup/pyMC_Repeater` → `HansvanMeer/pyMC_Repeater`) has consolidated under the OpenHop project name. The pip package names, module directory, imports, systemd unit and default config path all changed accordingly:

| Item | Before | After |
|---|---|---|
| Core pip package | `pymc_core` | **`openhop_core`** |
| Core module dir | `src/pymc_core/` | **`src/openhop_core/`** |
| Repeater pip package | `pymc_repeater` | **`openhop_repeater`** |
| Python imports | `from pymc_core…` | **`from openhop_core…`** |
| Systemd unit | `pymc-repeater.service` | **`openhop-repeater.service`** |
| Config directory | `/etc/pymc_repeater/` | **`/etc/openhop_repeater/`** |
| Fork branch used | `main` | **`dev`** |

Staying on the legacy names would strand the WM1303 overlay on an unmaintained snapshot; this release keeps us aligned with upstream while preserving the reproducible "forks are never modified, all WM1303 changes live in `overlay/`" model.

## Changes

### 1. Overlay migration (`overlay/`)

- Core overlay dir renamed: `overlay/pymc_core/src/pymc_core/` → `overlay/pymc_core/src/openhop_core/` (via `git mv`, history preserved).
- Every overlay `.py` rewrites `pymc_core` → `openhop_core` in imports and internal references (`__version__`, `__file__`, diagnostic paths, pip-uninstall helper strings). `pymc_repeater` references were left untouched intentionally.
- Overlay repeater files unchanged in name; imports switched from `openhop_core` where applicable.
- All overlay `.py` files pass `python3 -m py_compile`.

### 2. Install / upgrade / bootstrap scripts

- `install.sh`, `upgrade.sh`: `CORE_BRANCH`/`REPEATER_BRANCH` → `dev`; `CONFIG_DIR`/`LOG_DIR`/`DATA_DIR` and the systemd unit switched to the `openhop` naming. `INSTALL_BASE` intentionally stays at `/opt/pymc_repeater` to keep the ExecStart / venv / systemd sandbox paths stable across the migration; only the config, log, data and service identities change.
- Overlay copy paths and site-packages verification steps target `src/openhop_core/` and `import openhop_core`.
- A new `/etc` migration block copies legacy `/etc/pymc_repeater` into `/etc/openhop_repeater` using `cp -an` (no overwrite), so the JWT identity, `version` and `wm1303_ui.json` survive on the first upgrade. The legacy directory is left in place for rollback.
- The service update block disables and removes the legacy `pymc-repeater.service` and installs / enables the new `openhop-repeater.service`. During upgrades either unit may be the active one; both paths are handled.
- Cron watchdog file renamed to `/etc/cron.d/openhop-repeater-weekly-reboot`; the legacy file is cleaned up if present.
- `bootstrap.sh` picks up the new config path and detects an existing install via either the new `openhop-repeater` unit or the legacy `pymc-repeater` unit.

### 3. Repeater dependency resolution

The upstream `openhop_repeater` `pyproject.toml` pins `openhop_core[hardware] @ git+https://github.com/openhop-dev/openhop_core.git@dev`. To keep the fork model intact, the scripts install `openhop_core` first (editable, from the local `HansvanMeer/pyMC_core@dev` clone) and then install `openhop_repeater` editable with `--no-deps`, so the URL pin never triggers a re-fetch from openhop-dev. Runtime dependencies come from the existing venv.

### 4. Systemd unit (`config/openhop-repeater.service`)

- `Description`, `SyslogIdentifier` and install-header updated.
- `ExecStart --config` points at `/etc/openhop_repeater/config.yaml`.
- `ReadWritePaths` covers `/var/log/openhop_repeater /var/lib/openhop_repeater /etc/openhop_repeater`. On existing installs where the log and data directories still live under the legacy names, `upgrade.sh` transparently creates compatibility symlinks so the namespace sandbox is satisfied without moving any SQLite database.

### 5. Layer-2 protocol validator (from v2.5.10, verified on OpenHop)

The central Layer-2 protocol validator introduced in v2.5.10 continues to run on the new `openhop_core` stack unchanged. `Invalid Packets` forensics, drop-reason accounting and offender fingerprints all behave as before; the `invalid_packets_recent`, `invalid_packets_offenders` and `invalid_packets_stats` endpoints return the same shape as under the legacy stack.

## Files changed

- `VERSION` (2.5.10 → 2.6.0)
- `install.sh`, `upgrade.sh`, `bootstrap.sh`
- `config/pymc-repeater.service` → `config/openhop-repeater.service` (renamed + content updated)
- `overlay/pymc_core/src/pymc_core/**` renamed to `overlay/pymc_core/src/openhop_core/**` (10 files)
- 15 overlay repeater `.py` files with `pymc_core` → `openhop_core` import rewrites

## Breaking changes

- **Service name changes**: any external monitoring, cron entries, log-shippers or scripts referencing `systemctl … pymc-repeater` or `journalctl -u pymc-repeater` must be updated to `openhop-repeater`.
- **Config path changes**: tooling that reads or writes `/etc/pymc_repeater/*` directly (outside `install.sh` / `upgrade.sh` / `bootstrap.sh`) must switch to `/etc/openhop_repeater/*`. Existing content is copied over on first upgrade, but the legacy path is no longer the source of truth.
- **Python imports change**: any out-of-tree code doing `from pymc_core…` must switch to `from openhop_core…`.

## Verification (reference hardware)

1. `openhop-repeater.service` reports `active (running)` on the reference device after upgrade; the legacy `pymc-repeater.service` is `not-found` (disabled and removed).
2. `journalctl -b -u openhop-repeater` reports 0 `ModuleNotFoundError`, 0 `Traceback`, 0 residual `pymc_core` strings.
3. `openhop_core.__file__` and `openhop_core.hardware.wm1303_backend.__file__` resolve to `/opt/pymc_repeater/repos/pyMC_core/src/openhop_core/…` (editable install, repo IS the import source).
4. md5 of the six WM1303 overlay hardware modules (`wm1303_backend.py`, `tx_queue.py`, `sx1302_hal.py`, `sx1261_driver.py`, `region_config.py`, `virtual_radio.py`) matches the overlay source at the live import path.
5. Live RX / TX flow observed on the reference device: `RX CRC_OK` frames arrive at `WM1303Backend`, are forwarded by `BridgeEngine` to the repeater endpoint, and are transmitted back out via `ChannelTXQueue` with `TX_ACK` reporting `cad[clear] lbt[pass]` on the configured channels.
6. `GET /api/wm1303/status`, `/invalid_packets_recent`, `/invalid_packets_offenders`, `/invalid_packets_stats` all return HTTP 200 with well-formed JSON and the expected key sets (`packets`, `offenders`, `total_24h + per_reason + top_offender + top_offender_count + rate_per_hour`).
7. Only one repeater service is active after the upgrade (no duplicate legacy instance).

## Upgrade

Run the bootstrap one-liner or `sudo ./upgrade.sh` on a repeater that is currently on any v2.5.x release. The upgrade will:

1. Back up the current install (config, repos, packet-forwarder, service unit) under `/opt/pymc_repeater/backups/pre-upgrade-<timestamp>/`.
2. Migrate `/etc/pymc_repeater` → `/etc/openhop_repeater` (JWT, `version`, `wm1303_ui.json`, `presets.json`, `config.yaml*` preserved).
3. Check out `dev` on both forks, `pip install -e` `openhop_core[hardware]`, then `pip install -e . --no-deps` for `openhop_repeater`, and apply the WM1303 overlay on the new module tree.
4. Disable and remove `pymc-repeater.service`, install and enable `openhop-repeater.service`, `daemon-reload`, start and verify.

No database migration is required. The SQLite databases for metrics and `invalid_packets` are kept in place; compatibility symlinks (created during the upgrade) let the new unit's `ReadWritePaths` reach them.

## Rollback

Rollback is possible from the pre-upgrade backup dir; the previous release's rollback recipe still applies with the openhop unit substituted:

```bash
sudo systemctl disable --now openhop-repeater
sudo rm -f /etc/systemd/system/openhop-repeater.service
sudo cp <backup>/pymc-repeater.service.bak /etc/systemd/system/pymc-repeater.service
sudo tar xzf <backup>/repos_pyMC_core.tgz -C /opt/pymc_repeater/repos/
sudo tar xzf <backup>/repos_pyMC_Repeater.tgz -C /opt/pymc_repeater/repos/
sudo /opt/pymc_repeater/venv/bin/pip uninstall -y openhop_core openhop_repeater
cd /opt/pymc_repeater/repos/pyMC_core && sudo /opt/pymc_repeater/venv/bin/pip install -e .
cd /opt/pymc_repeater/repos/pyMC_Repeater && sudo /opt/pymc_repeater/venv/bin/pip install -e . --no-deps
sudo systemctl daemon-reload && sudo systemctl enable --now pymc-repeater
find /opt/pymc_repeater/repos -name __pycache__ -exec rm -rf {} +
```

The legacy `/etc/pymc_repeater` directory is intentionally left in place after the migration so a rollback can point at it directly.

## Known issues / follow-ups

- `/var/log/openhop_repeater` and `/var/lib/openhop_repeater` are compatibility symlinks to the legacy directories on upgraded devices. A future release can flip that so the openhop paths become the real directories and the pymc paths become the symlinks; this is deferred to avoid touching the SQLite databases in this release.
- The `invalid_packets_stats` endpoint still rejects an unused `limit` query parameter (unchanged from v2.5.10; the UI does not send it).
