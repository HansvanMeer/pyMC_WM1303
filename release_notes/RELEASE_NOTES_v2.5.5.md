# Release Notes — v2.5.5

**Release date:** 2026-06-14  
**Status:** Stable  
**Commit:** [`26d5639`](https://github.com/HansvanMeer/pyMC_WM1303/commit/26d5639)

---

## Summary

v2.5.5 brings the long-awaited **Neighbours tab** with three feature packs (UX, functional, and heavy features), plus important improvements to **spectrum scanning** (region-aware), **noise floor measurement** (median + outlier rejection), and **watchdog recovery** (auto-escalation to deep reset).

Addresses GitHub Issues **#7.1**, **#7.2**, **#11.1**, and **#11.2**.

---

## Highlights

### Neighbours Tab — Full Feature Pack (Issue #11.2)

New dedicated tab to monitor MeshCore nodes heard via ADVERT packets, with three integrated feature packs:

**Pack 1 — UX upgrades**
- Stats cards: Total, Active (<5 min), Strongest RSSI, Weakest RSSI
- Sortable column headers (Name, Last seen, RSSI, SNR, Adverts)
- Live search/filter box on friendly_name or node_id
- Last-seen quick filter: All / <5 min / <1 h / <24 h
- Auto-refresh toggle (15 s interval, off by default)

**Pack 2 — Functional additions**
- CSV export of current (filtered) table
- Per-node detail modal with full info
- Bulk-ping selected nodes via checkbox selection

**Pack 3 — Heavy features**
- Per-node RSSI/SNR history line chart (24 h ring buffer, max 200 samples)
- Topology view (Chart.js bubble chart, X=SNR, Y=RSSI, size=advert count)
- Per-channel filter

**Endpoints added**
- `GET /api/wm1303/neighbours` — list with aggregated state
- `GET /api/wm1303/neighbours/<node_id>/history` — 24 h ring buffer samples

### Region-Aware Spectrum Scan (Issue #7.1)

Replaces the hard-coded EU868 spectrum range with a configurable region preset system.

**Supported regions:** EU868, US915, AU915, AS923, IN865, JP920, KR920, CUSTOM

Spectrum endpoint now returns:
```json
{
  "freq_range": {
    "start_mhz": 863.0,
    "stop_mhz": 870.0,
    "step_khz": 200.0,
    "region": "EU868"
  }
}
```

UI spectrum chart title now dynamically shows the active range and region.

### Noise Floor Median Filter + Outlier Rejection (Issue #7.2)

Replaces unstable mean-based noise floor calculation with **median + outlier rejection**.

- `NF_VALID_MIN_DBM = -130` and `NF_VALID_MAX_DBM = -60` — values outside this range are rejected as outliers
- Median (instead of mean) provides much more stable noise floor readings, particularly in noisy RF environments
- Channels tab now shows stable noise-floor values without sporadic spikes

### Watchdog Auto-Escalation (Issue #11.1)

The RX watchdog now tracks consecutive restart attempts. When the regular service restart fails to restore RX traffic, the system **automatically escalates to a deep hardware reset** without manual intervention.

- New `_consecutive_watchdog_restarts` counter in backend
- After 2 consecutive restarts without RX recovery → auto-trigger `deep_reset` (GPIO power-cycle of the LGW chip)
- Counter resets to 0 once RX traffic is detected
- Eliminates need for manual scripted deep resets for the most common stuck states

---

## Files Changed

| File | Change |
|------|--------|
| `VERSION` | `2.5.4` → `2.5.5` |
| `overlay/pymc_core/src/pymc_core/hardware/tx_queue.py` | NF outlier-filter + median (Issue #7.2) |
| `overlay/pymc_core/src/pymc_core/hardware/wm1303_backend.py` | Watchdog auto-escalation (Issue #11.1) |
| `overlay/pymc_repeater/repeater/web/wm1303_api.py` | Region presets, `_get_spectrum_scan_range()`, `_neighbours_get()`, `_neighbours_history_get()`, ring buffer |
| `overlay/pymc_repeater/repeater/web/html/wm1303.html` | Pack 1+2+3 Neighbours tab UI, dynamic spectrum title (~400 new lines) |

---

## Upgrade Instructions

```bash
curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
```

After upgrade, **hard-refresh the browser** (`Ctrl + Shift + R` / `Cmd + Shift + R`) to load the new UI assets.

---

## Known Limitations

- Neighbours ring buffer is **in-memory only** in v2.5.5 — data is lost on service restart. Persistence is planned for v2.5.7.
- Per-channel filter UI works on data tagged in this release; data captured by previous versions has no channel attribution.
- Location/GPS information is **not yet parsed** from ADVERT packets — planned for v2.5.7 (map view).

---

## Tested On

- pi01 (192.168.101.52) — Raspberry Pi OS Lite — bootstrap upgrade verified
- pi03 (192.168.101.80) — Raspberry Pi OS Lite — bootstrap upgrade verified

Both devices verified active service, version 2.5.5, and working endpoints (`status`, `neighbours`, `spectrum`).
