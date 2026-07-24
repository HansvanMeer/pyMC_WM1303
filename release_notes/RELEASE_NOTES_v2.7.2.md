# pyMC_WM1303 v2.7.2 — Release Notes

**Release date:** 2026-07-24
**Type:** Patch (bug fixes + structural prevention, no breaking changes)
**Upgrade:** Safe drop-in over v2.7.1 via the standard bootstrap one-liner.

## Summary

Four bug fixes and one structural install-time safeguard that together restore per-packet metric persistence, Layer-2 protocol validation storage, per-event CRC drill-down and channel-stats retention — all on the WM1303 code path — plus a new deploy-gap check that catches missing overlay files during install/upgrade.

## Fixes

### #211 — SQLite deploy-gap: missing `invalid_packets` / `packet_metrics` tables and `store_packet_metric`

- **Symptom:** recurring `WARNING: 'SQLiteHandler' object has no attribute 'store_packet_metric'`; the Manager 'Invalid packets' tab always empty because the `invalid_packets` table did not exist in the active database.
- **Root cause:** `install.sh` / `upgrade.sh` had no copy-loop for `repeater/data_acquisition/`, so devices ran the bare upstream fork `sqlite_handler.py` without the WM1303 tables and without `store_packet_metric`.
- **Fix:** rebuilt `overlay/pymc_repeater/repeater/data_acquisition/sqlite_handler.py` on top of the fork (kept all 9 fork-only methods to avoid a `models.py`-style drift regression) and re-applied the WM1303 delta: 6 idempotent tables in `_init_database` (`packet_metrics`, `crc_error_rate`, `dedup_events`, `sx1261_health_events`, `neighbour_samples`, `invalid_packets`) + `invalid_packet_offenders` view + `set_invalid_packet_store` wiring + 23 WM1303 methods (including `store_packet_metric` and `store_invalid_packet`) + WAL-checkpoint thread. Added a `data_acquisition/` copy-loop to `install.sh` + `upgrade.sh`.
- **Impact:** restores per-packet metric persistence and the 'Invalid packets' tab storage on every device.

### #212 — `crc_errors` per-event table not populated (only `crc_error_rate` aggregate was growing)

- **Symptom:** the per-event `crc_errors` table stayed at 0 rows while `RX CRC_ERROR` events appeared in the journal; only the per-minute aggregate `crc_error_rate` grew.
- **Root cause:** the fork-side writer `engine._record_crc_errors_async()` reads `radio.crc_error_count` — a KISS/SX126x-modem attribute that the WM1303 dispatcher does not expose — so `delta` was always 0 and no INSERT ever happened.
- **Fix:** in `overlay/pymc_repeater/repeater/web/wm1303_api.py` (`_record_crc_error_rate_once`), accumulate `total_crc_errors` across all channels after the existing `INSERT INTO crc_error_rate ...`; when >0, add one `INSERT INTO crc_errors (timestamp, count)` per 60-s cycle on the same DB connection. The RX hot-path is **not** touched — this runs in the existing background recorder thread.
- **Impact:** enables per-event CRC drill-down persistence without any RX throughput cost.

### #213 — `metrics_retention` failed on `channel_stats_history` with `no such column: pkt_count`

- **Symptom:** recurring `metrics_retention WARNING … channel_stats_history failed: no such column: pkt_count`; the retention pass aborted so old rows in that table were never pruned (unbounded growth).
- **Root cause:** `pkt_count` is a dead column — no writer inserts it into `channel_stats_history`. The authoritative RX-per-channel source is `packet_activity`; `pkt_count` only exists as a computed output field in API JSON responses.
- **Fix:** query-correction in `overlay/pymc_repeater/repeater/metrics_retention.py` and `overlay/pymc_repeater/repeater/web/tiered_query.py` — the dead column was removed from the rollup SELECT list. No schema change, no dead-column resurrection.
- **Impact:** restores rollup and retention on `channel_stats_history` (was growing unbounded); the 1m/10m/15m rollup tables populate normally again.

### #214 — Layer-2 protocol validator not wired into RX-flow (`invalid_packets` always 0 despite RX volume)

- **Symptom:** `invalid_packets` remained 0 rows despite thousands of RX packets; 0 validator activity in the journal.
- **Root cause:** deploy-gap — `install.sh` / `upgrade.sh` did not include `protocol_validator.py` in the overlay-copy list, so the file was missing entirely from the active directory. `wm1303_backend.py` imports `from repeater.protocol_validator import validate_and_record` inside a `try/except` that silently sets `validate_and_record = None` on `ModuleNotFoundError`, so the RX-callback never called the validator.
- **Fix:** added `"protocol_validator.py"` to the overlay-copy list in both `install.sh` and `upgrade.sh`. No code changes needed — the overlay code was already correct.
- **Impact:** restores Layer-2 protocol validation persistence for every WM1303 device.

### #215 — Install-time deploy-gap verification (structural prevention of #211/#214-style bugs)

- **What:** added a ~15-line verification block at the end of both `install.sh` and `upgrade.sh` (immediately before `systemctl start openhop-repeater`) that compares `overlay/pymc_repeater/repeater/*.py` against `${RPT_DIR}/repeater/*.py` (fork-checkout served via PYTHONPATH) using `comm -23` on sorted find lists. `storage_collector.py` is excluded via regex (documented fork-drift keep-out).
- **Behaviour:** on mismatch it emits a `warn` (not `fail`, so it never blocks a legitimate install/upgrade) with a per-file bullet list and a hint to add the missing file to the appropriate for-loop. On match it emits an `ok` confirming full coverage.
- **Impact:** any future forgotten overlay `.py` file is caught during the next install/upgrade run with a clear warning naming the missing file, instead of surfacing months later as a silent behavioural bug.

## Verification

All fixes deployed to a test device running v2.7.1 and verified: service `active (running)`, `NRestarts=0`, no new tracebacks. `packet_metrics` growing live, `crc_errors` growing per 60-s cycle, `invalid_packets` growing with real Layer-2 protocol drops, `channel_stats_history` retention pruning correctly, rollup tables populated. Bootstrap one-liner on a v2.7.1 device applies all fixes cleanly.

## Upgrade

Standard bootstrap one-liner from README/docs. No manual steps required. Configuration files in `/etc/openhop_repeater/` are preserved.

## Files changed vs v2.7.1

- `overlay/pymc_repeater/repeater/data_acquisition/sqlite_handler.py` (#211 rebase)
- `overlay/pymc_repeater/repeater/web/wm1303_api.py` (#212)
- `overlay/pymc_repeater/repeater/metrics_retention.py` (#213)
- `overlay/pymc_repeater/repeater/web/tiered_query.py` (#213)
- `install.sh` (#211 + #214 copy-loops, #215 verification block)
- `upgrade.sh` (#211 + #214 copy-loops, #215 verification block)
- `VERSION` (2.7.1 → 2.7.2)
- `TODO.md`
- `release_notes/RELEASE_NOTES_v2.7.2.md` (new)
