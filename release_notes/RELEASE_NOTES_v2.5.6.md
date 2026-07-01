# Release Notes — v2.5.6

**Release date:** 2026-06-14  
**Status:** Stable (patch release)  
**Commit:** [`f569911`](https://github.com/HansvanMeer/pyMC_WM1303/commit/f569911)

---

## Summary

Small but meaningful **patch release** for `upgrade.sh`. The Phase 11.4 web-interface availability check is now reliable and honest: it retries before giving up, and reports a real warning (not a misleading `ok`) when the web server fails to respond.

---

## What changed

### Phase 11.4 retry loop + correct warning on failure

**Before (v2.5.5)** — a single one-shot check that always logged a green `ok`, even when the web interface was not responding:

```bash
if curl ... ; then
    ok "Web interface responding"
else
    ok "Web interface not yet responding (may need a few more seconds)"  # ← misleading
fi
```

**After (v2.5.6)** — 5-attempt retry loop with 2-second intervals (10 s total wait), correct `warn` on failure, and an actionable troubleshooting hint:

```bash
for WEB_TRY in 1 2 3 4 5; do
    if curl ... ; then WEB_OK=1; break; fi
    sleep 2
done
if [ "${WEB_OK}" = "1" ]; then
    ok "Web interface responding on port ${WEB_PORT} (ready after ${WEB_TRY} attempt(s))"
else
    warn "Web interface not responding after 5 attempts (10s) - check: journalctl -u pymc-repeater"
fi
```

### Three bugs fixed at once

1. **Logical bug** — the old code used `ok` on the failure path, leading users to believe the upgrade succeeded when the web server was actually not yet responding.
2. **No retry** — the old code did a single check after a 2 s sleep. Slower SD cards / Pi models often needed more time and were falsely reported.
3. **No actionable hint** — when something genuinely went wrong, the user got no pointer. Now: `check: journalctl -u pymc-repeater`.

---

## Files Changed

| File | Change |
|------|--------|
| `VERSION` | `2.5.5` → `2.5.6` |
| `upgrade.sh` | Phase 11.4 — replace one-shot check with 5x retry loop + correct `warn` (lines 2015-2030) |

Total diff: 2 files, +14 -4 lines.

---

## Verification

Deployed via bootstrap on the reference device and the test device:

| Device | Phase 11.4 Result | Service | Version Endpoint |
|---|---|---|---|
| **the reference device** (the reference repeater) | `ready after 1 attempt(s)` | active | 2.5.6 |
| **the test device** (the test repeater) | `warn after 5 attempts (10s)` | active (started ~12 s after Phase 11.4 check) | 2.5.6 |

the test device demonstrates exactly the failure mode the fix targets: the warn is **accurate** (the web server was indeed not responding within 10 s) and **actionable** (`journalctl -u pymc-repeater`), while the service did eventually start successfully. Under v2.5.5 this same situation would have logged a misleading `ok`.

---

## Upgrade Instructions

```bash
curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
```

No configuration changes are required; this release only modifies the upgrade-script reporting logic.

---

## Notes for slower devices

If you see the new `warn` on a device with slow startup (e.g., older SD cards, busy I/O), increase the retry count in `upgrade.sh` (currently 5 × 2 s = 10 s). A future patch may raise this default to 10 × 2 s = 20 s based on real-world feedback.
