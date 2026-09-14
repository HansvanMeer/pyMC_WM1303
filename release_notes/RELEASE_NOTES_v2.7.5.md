# Release Notes — v2.7.5

**Release date:** 2026-09-14
**Type:** Minor release (metrics integrity, tracing accuracy, chart fixes)

This release is about one thing: making the Manager UI tell the truth.

Several charts and counters looked healthy while the data behind them was
missing, mislabelled or silently zeroed. A calm CAD chart hid tens of thousands
of channel-busy retries. An `LBT RSSI` line was advertised in a legend but could
never be drawn, while the database column behind it held a noise-floor reading
that no LBT check had ever produced. A wait-time series was dropped during
rollup. Trace rows displayed hardcoded constants in a column that implied they
were measurements.

Every fix in this release was verified against live data on reference hardware,
in the rendered page rather than in the JSON payload.

---

## Summary

| Area | Item | Status |
|------|------|--------|
| Metrics rollup | Tiered downsampling deleted source rows without aggregating them | ✅ Fixed |
| Metrics rollup | Cumulative counters aggregated to zero (`MAX−MIN` on single-sample buckets) | ✅ Fixed |
| Metrics rollup | Read path still used the same `MAX−MIN` delta, causing a step at the tier boundary | ✅ Fixed |
| Metrics rollup | New aggregation columns could never reach an existing database | ✅ Fixed |
| Channel stats | Frozen average RSSI/SNR and permanently NULL noise-floor columns | ✅ Fixed |
| CAD | Busy detections invisible in channel statistics | ✅ Fixed |
| CAD | `CAD Activity per Channel` could not show retries at all | ✅ Fixed |
| LBT | `LBT RSSI` series structurally empty, and the stored value was not an LBT measurement | ✅ Fixed |
| TX metrics | `TX Airtime & Wait Time` wait series dropped during rollup | ✅ Fixed |
| Routing | Channel messages delivered to the companion once per received copy | ✅ Fixed |
| Tracing | Step order not guaranteed for back-dated events | ✅ Fixed |
| Tracing | Duration badge and Δ column derived from two different clocks | ✅ Fixed |
| Tracing | Δ column mixed four meanings; constants presented as measurements | ✅ Fixed |
| Tracing | Multi-second silences between phases were invisible | ✅ Fixed |
| Spectrum | `specLoad()` aborted halfway on every tab open | ✅ Fixed |
| Install | Stale `openhop` database with an old schema crashed the snapshot loop | ✅ Fixed |
| API | Unknown or legacy query parameters caused HTTP 500 | ✅ Fixed |
| Install | Stale legacy version marker left behind by old installs | ✅ Fixed |
| Storage | Journal and database growth unbounded on long-running devices | ✅ Fixed |
| UI | Dutch strings in an English-only interface | ✅ Fixed |
| UI | Decimal comma instead of point in frequency fields | ✅ Fixed |

---

## Metrics integrity

### Tiered downsampling destroyed data instead of aggregating it

**Symptom.** Long-term graphs were empty beyond the hot window, while the hourly
log line claimed rows had been aggregated.

**Root cause.** The aggregation routines used a window-wide existence probe: if
*any* row already existed in the target window, the whole window was treated as
done and the source rows were deleted. From the second cycle onward every new
row was discarded without ever being aggregated.

**Fix.** Per-bucket idempotency. Summary tables now carry a
`UNIQUE(bucket_ts, <group cols>)` index and writes use `INSERT OR IGNORE`, so
re-running a window is safe without discarding anything. The counter feeding the
log line was also reporting deleted rows as if they were aggregated buckets; it
now reports buckets actually written.

### Cumulative counters aggregated to zero

**Symptom.** RX/TX counts, airtime and LBT counters were zero in every rolled-up
bucket, so history graphs flatlined outside the hot window.

**Root cause.** Counters in `channel_stats_history` are monotonic since service
start and were differenced *within* a bucket using `MAX(x) − MIN(x)`. The
snapshot loop writes that table once per 60 s, so a 60 s bucket holds exactly one
sample and the expression is zero by construction. Coarser buckets only appeared
to work: they still dropped every increment falling between the last sample of
one bucket and the first of the next.

**Fix.** Cumulative columns are marked with a `CUMDELTA:` prefix and differenced
across bucket boundaries with a `LAG()` window function, clamped with `MAX(0, …)`
to absorb counter resets on restart. Bucket alignment and LAG-seed retention were
added so a partial trailing bucket is neither written from incomplete input nor
lost, removing a systematic undercount of roughly 1.7 % per cycle.

### The read path used the same broken delta

The hot-tier query still used `MAX(x) − MIN(x)`. With the write side corrected, a
single series would have shown a visible step exactly at the hot/warm boundary —
correct data on one side, zeroes on the other, which reads as a hardware or
traffic anomaly rather than a query bug. The read side now uses the identical
`CUMDELTA` + `LAG()` convention. **Both modules must stay in sync**; a mismatch
reintroduces the step.

### New aggregation columns could never reach an existing database

`_create_summary_table()` used `CREATE TABLE IF NOT EXISTS`, which is a no-op
once the table exists. Any newly added aggregation column was therefore missing
on every already-deployed device, and each INSERT failed with `no such column`.
A generic `PRAGMA table_info` + `ALTER TABLE ADD COLUMN` migration now runs on
every cycle, so extending the aggregation set stays a one-line change. This trap
had already cost two separate fixes before being addressed structurally.

### Frozen average RSSI/SNR and NULL noise-floor columns

Three independent defects in the backend:

- `avg_rssi` / `avg_snr` were computed over never-reset lifetime accumulators.
  Past a few hundred thousand packets a new sample no longer moves that mean by
  the stored precision, so the value froze — 438 rows held only 14 distinct RSSI
  values. A rolling per-channel sample window now averages over the snapshot
  period, and a quiet channel stores NULL instead of repeating a stale reading.
- `noise_floor_dbm` was NULL on every row because the lookup mapped UI channel
  names onto `channel_a..channel_d` only, while the active channel is
  `channel_e`.
- `tx_noisefloor_dbm` was NULL on every row because the stats call never
  forwarded the key that the TX queue already computes.

---

## CAD and LBT

### CAD busy detections were invisible

`cad_clear` / `cad_detected` recorded only the final outcome of a transmission.
A TX that found the channel busy, retried and then succeeded was recorded as
clear, so the statistics reported a perfectly quiet channel. Four counters were
added to the TX queue (`cad_busy_events`, `cad_retry_total`,
`cad_tx_with_retries`, `cad_max_retries`), fed from the post-TX acknowledgement
and exposed through the per-channel payloads, the live CAD block and the
`cad_events` history table.

### The CAD chart still could not show them

The counters were stored but never surfaced anywhere in the interface.
`Detected` counts only a transmission that still saw activity after *all* retries
and was force-sent anyway; since the channel always frees up eventually it stayed
at zero, and the chart showed a calm channel while the radio had found the air
busy tens of thousands of times.

The retry total is now aggregated, served per bucket and drawn as a
`Busy (retries)` line on its own axis, with the retry counts also shown on the
per-channel stat cards.

### LBT RSSI was a promise the chart could not keep

Two independent defects:

- The row mapper hardcoded `lbt_last_rssi` to `None` with the comment *not
  available in aggregated data*. The column was simply missing from the
  aggregation set, so the series could never carry a value at any time range,
  while the legend advertised it.
- More seriously, the value stored in that column was **not** an LBT
  measurement. `record_lbt_rssi()` assigned it, and that method is also fed by
  the SX1261 noise-floor monitor. On a channel with LBT switched off, every row
  carried a noise-floor reading labelled as LBT. All affected rows had
  `lbt_blocked = 0` **and** `lbt_passed = 0`, proving no LBT check had ever run.

The assignment was removed from the noise-floor path — the genuine LBT path sets
the field itself — and the aggregation now serves the real column. When a channel
produced no LBT data the series and its legend entry are omitted, and the chart
states that LBT is disabled on that channel instead of implying a missing
measurement.

### TX wait time was dropped during rollup

`TX Airtime & Wait Time per Channel` drew a single line. The value is measured
and stored correctly, but `wait_time_ms` was missing from the aggregation set and
the bucket field was initialised to zero and then never accumulated, while the
airtime field right next to it was. Because the chart filters points at zero, the
entire series disappeared.

The caption also claimed the dashed line covered *queue + CAD + guard*, which it
never did — it is queue time only. CAD scan and RF guard are separate steps,
visible per packet in the Tracing tab. Caption corrected.

---

## Packet tracing

### Step order was not guaranteed

`trace_event()` appended unconditionally, so a back-dated event could land out of
chronological order and corrupt every derived timing. Insertion is now ordered,
with a guard ensuring the reported total never falls below the largest elapsed
value.

### Two clocks for one scan

The duration badge and the Δ column were derived from independent sources and
disagreed by several milliseconds on the same row. Both now come from the single
HAL-reported value.

### The Δ column mixed four meanings

The delta helper returned a different kind of value per step: a hardware
duration, a hard zero for "instantaneous" markers, a forward-looking scan
duration, or a backward-looking launch delay. A trace therefore did not close
when read from top to bottom.

Measured over 50 live traces, rows marked as instantaneous displayed 0.0 ms while
2352.3 ms of real time had elapsed, and rows marked as measured displayed
2460.0 ms against only 67.2 ms of actual gap. The two errors nearly cancelled
(net 0.1 %), so the column appeared to add up while individual rows were wrong by
tens of milliseconds — the most misleading possible outcome, because checking the
sum confirmed a total that the individual rows did not support.

Worse, three Δ values in every TX trace are fixed constants rather than
measurements, each with a spread of only 0.1 ms across 24 traces. The reason is
legitimate — the packet forwarder only reports back at TX acknowledgement, so
every earlier TX-phase moment is reconstructed from that single anchor — but the
result was presented as if measured.

The column now always shows the plain gap since the previous row, so the sum
equals the trace total exactly. The HAL scan duration and computed airtime remain
visible as detail badges, so no information is lost. Reconstructed timings are
marked with a leading `~` on both step rows and wait rows, with a per-row
explanation and a footer legend.

### Silences between phases were invisible

A trace could jump from `TX complete` straight to the next `RX received` with
nothing indicating that seconds had passed. An `idle` separator is now rendered
between phase blocks when the silence reaches 100 ms. The threshold was measured
rather than guessed: inter-phase gaps are strictly bimodal, with in-phase
transitions between 2.4 and 3.9 ms and idle periods between 685 and 3238 ms, so
any threshold in that range yields the same result.

---

## Packet routing

### Channel messages arrived at the companion once per received copy

A `GRP_TXT` message that reached the node over more than one path was pushed to
the companion once for every copy received, so the same channel message appeared
several times in the client.

`_dispatch_received()` applied the companion dedupe window to `PATH` and
protocol-response packets only, while the `GRP_TXT` branch forwarded
unconditionally. Flood-routed channel messages arrive once per repeater in range,
and on WM1303 hardware every RF packet is enqueued on the router *before* the
engine's duplicate check, so each copy reached the companion.

The existing guard is now applied to `GRP_TXT` as well, reusing the same dedupe
window. Repeater forwarding is unaffected.

---

## Installation and robustness

### Stale database quarantine

A `repeater.db` left behind by an older install could carry a schema without the
columns the current snapshot loop writes, producing a recurring
`table channel_stats_history has no column named rx_count` crash and permanently
empty graphs. Both `install.sh` and `upgrade.sh` now detect that case and
quarantine the file so a fresh database is created at first start.

### API hardening

Query-string endpoints returned HTTP 500 when an unknown or legacy parameter
arrived — a stale browser tab sending an obsolete parameter was enough to break a
graph endpoint. Public handlers now absorb unexpected parameters, and
`default()`-dispatched handlers are invoked through a wrapper that filters
parameters against the handler signature. Both layers together make the failure
mode structurally impossible.

### Stale legacy version marker

Old installs left a legacy version file behind that could be mistaken for the
canonical version during audits. Both scripts now re-sync an existing legacy
marker — never creating one — so the canonical path stays the single source of
truth.

### Storage growth

Journal limits are installed as a systemd drop-in (200 MB cap, 8-day retention)
alongside the existing database retention, so a device running for weeks no
longer fills its SD card.

---

## Interface

- **English-only.** Fourteen Dutch tooltips and one visible label remained in the
  tracing tab of an otherwise English interface. All replaced.
- **`specLoad()` no longer aborts.** An element removed from the markup during an
  earlier cleanup still had an unguarded assignment, throwing on every Spectrum
  tab open and silently skipping the spectral-scan enable checkbox and the LBT
  channel list. The missing guard was added.
- **Decimal separator.** Frequency fields in the RF-chain and IF-chain panels
  used a comma; they now use a point.
- **Chart axis correctness.** Adding a second y-axis to the CAD chart silently
  moved the event bars onto it, because Chart.js falls back to the first declared
  scale when a dataset has no explicit axis. Both bar datasets now declare their
  axis explicitly.

---

## Upgrade notes

No configuration changes are required. `upgrade.sh` handles everything, including
the database column migrations, which run automatically on the first retention
cycle after start.

Devices that have been running with LBT disabled will find their historical
`LBT RSSI` values cleared. Those values were noise-floor readings stored under an
LBT label and were never valid LBT measurements; the noise-floor and RX values
they were derived from are untouched and remain available in their own series.

---

## Known open items

| Item | Description |
|------|-------------|
| #241 | The HAL-reported CAD scan duration is systematically 0.8–10.7 ms larger than the gap between scan start and scan result on the same timeline. Roughly 1 ms is structural (a fixed scan-result offset); the remainder comes from clamping the reconstructed scan start. Both numbers are now shown side by side and explicitly marked as reconstructed, so nothing is misleading, but they should eventually agree. |
| #10 | Long-running memory-leak check. Tooling is ready; this is an operational task requiring an extended observation window. |
