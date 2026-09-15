# Release Notes — v2.7.6

Maintenance release. Six defects in `install.sh` and `upgrade.sh` that made a
perfectly healthy upgrade report itself as broken. No functional change to the
repeater, the radio stack or the web interface; the fixes are confined to the
install and upgrade scripts and their verification phase.

The common theme is that every one of these defects produced a **false negative**
on a correctly working system. Together they meant a successful upgrade ended
with a scary warning, an unusable URL, a troubleshooting banner for hardware
that was transmitting fine — or simply stopped halfway with a non-zero exit code
and no explanation at all.

---

## #247 — Upgrade aborted silently at phase 11.6 on a healthy system

**Symptom.** A fully successful upgrade stopped producing output directly after
`[11.6] Checking journal for post-startup errors ...`. Phase 11.7 and the final
summary never ran and the script exited non-zero.

**Root cause.** The script runs under `set -euo pipefail`. The journal check
assigned the result of a pipeline ending in `grep -v "^-- "`. On a system with no
errors `journalctl` emits only `-- No entries --`; that line is filtered away,
`grep` exits 1 because nothing matched, `pipefail` promotes this to the pipeline
status, the command substitution inherits it and `set -e` terminates the script.

The failure is inverted: it triggers **precisely when the service is healthy**.
That is why it survived every earlier release, where the first 30 seconds of
journal still contained startup errors.

**Fix.** A `|| true` guard on the substitution, with an inline comment recording
why removing it breaks the upgrade.

**Impact.** The upgrade now runs to completion and exits 0.

---

## #248 — Dead `pkt_count` column re-created, with a schema that the stale-DB quarantine treats as corrupt

**Symptom.** Every upgrade reported `added channel_stats_history.pkt_count`.

**Root cause.** Two coupled defects in the schema block:

- The column-migration list still carried `('channel_stats_history', 'pkt_count',
  'INTEGER')`, re-adding a column that was deliberately removed from all queries
  in #213. Measured on a live database it held 435 rows with **zero** non-null
  values, while `rx_count` beside it was populated on all 435. RX-per-channel is
  derived from `packet_activity`; `pkt_count` survives only as a computed field
  in the API JSON.
- More seriously, the `CREATE TABLE IF NOT EXISTS` definition declared
  `pkt_count` and omitted `rx_count` entirely, diverging from
  `_init_channel_stats_db()` in `wm1303_backend.py`. Whenever the script created
  the table before the backend did, it produced exactly the shape that makes
  every 60-second snapshot INSERT fail with `no column named rx_count` — and that
  the stale-DB quarantine from #228 then classifies as corrupt and moves aside.
  The script could therefore manufacture the very failure #228 exists to repair.

**Fix.** The migration entry was dropped and the table definition replaced with
the backend schema verbatim. Both carry comments stating that the two must stay
identical. Fixing either half alone would have left the conflict intact.

**Impact.** No dead column is created, and the scripts can no longer generate a
database that their own repair logic quarantines.

---

## #249 — Empty `grep` output could abort both scripts before their own fallback applied

**Symptom.** Latent; not observed in the field.

**Root cause.** Both scripts resolved the web port from a `grep` pipeline whose
result was assigned directly, immediately followed by a `${WEB_PORT:-8000}`
default. Under `set -euo pipefail` a `config.yaml` without an explicit `port:`
key makes `grep` exit 1 and aborts the script one line before the default that
exists to handle exactly that case — the fallback was unreachable by
construction.

**Fix.** Found by scanning all three scripts for assignments from pipelines
containing commands that exit non-zero as a normal outcome (`grep`, `pgrep`,
`diff`, `cmp`, `curl`, `md5sum`, `systemctl is-active`) without a guard. Three
occurrences existed in total, the third being #247. All now carry `|| true`; a
rescan reports zero remaining and `bash -n` passes on all three scripts.

**Impact.** A hand-edited or partially migrated configuration can no longer abort
an upgrade.

---

## #250 — Concentrator detection in `install.sh` read the superseded systemd unit

**Symptom.** A fresh install reported `Concentrator module not yet confirmed`
even when the SX1302 had come up correctly.

**Root cause.** The check read `journalctl -u pymc-repeater`, the unit name
retired during the openhop migration. That unit no longer exists, so the query
always returned an empty log; neither the success nor the error pattern could
ever match, leaving the warning branch as the only reachable outcome. The
equivalent check in `upgrade.sh` already used the current name.

**Fix.** Corrected to `openhop-repeater`. The remaining `pymc-repeater`
references in both scripts were reviewed and are legitimate legacy cleanup that
stops, disables and removes the old unit.

---

## #251 — Web interface check probed the MQTT port instead of the web port

**Symptom.** Phase 11.5 reported `Web interface not responding` on every upgrade,
and the closing summary printed the web UI URL with the wrong port.

**Root cause.** The port was resolved with `grep -oP '^\s*port:' ... | head -1`,
which returns the **first** `port:` key in `config.yaml`. That file lists
`mqtt_brokers:` before `web:`, so the value picked up was the broker port 1883
and never the web port 8000. Probing 1883 over HTTP can never return 200, so the
check was guaranteed to fail regardless of how long it waited — the retry window
was never the real constraint.

**Fix.** An `awk` scan that enters the `web:` section and accepts `port:` only
inside it, falling back to 8000 when the section is absent. The retry loop is
kept because it costs nothing and covers a genuinely slow bind.

**Impact.** Verified on a live device: the old expression returned `1883`, the
replacement returns `8000`, and the interface now answers on the first attempt.
The summary prints a URL that works.

---

## #252 — Concentrator detection sampled a fixed time window and reported a working SX1302 as missing

**Symptom.** Phase 11.7 ended in `CONCENTRATOR MODULE NOT DETECTED`, complete
with a GPIO and SPI troubleshooting banner, while the radio was demonstrably
transmitting.

**Root cause.** The check slept 10 seconds and then searched a hardcoded
`--since '90 seconds ago'` window exactly once. Whenever the service took longer
to become ready than the window was wide, the query returned the *previous*
instance's startup lines, or none at all. The match patterns were never wrong:
the journal contains `pktfwd ready`, `lora_pkt_fwd started` and `backend started`,
and a manual replay of the same expression matched them.

**Fix.** A retry loop that anchors the journal window to the unit's
`ActiveEnterTimestamp` and re-reads that anchor on every attempt, so it follows a
restart occurring mid-loop. Per-attempt diagnostics (anchor value and captured
line count) are written to the log file, never to the console.

**Impact.** A full upgrade run now reports `detected and running (confirmed after
1 attempt(s))`.

**Known limitation.** One intermediate test run failed all attempts even though
the markers were present and the anchor was correct. That run could not be
explained afterwards because no diagnostics existed yet. The diagnostics added
here exist specifically so that a recurrence can be traced.

---

## #253 — `bootstrap.sh` always exited 143, discarding the real result of the install

**Symptom.** Every bootstrap run ended with exit code 143, whether the
installation succeeded or failed.

**Root cause.** `run_protected()` starts the install under `nohup`, tails the log
so the user sees live progress, and after `wait $BGPID` stores the real status in
`EXIT_CODE`. It then kills the tail and calls `wait $TAILPID`, which reports 143
(128 + SIGTERM) for the process it has just terminated. The script runs under
`set -e`, so execution stopped on that line and `return $EXIT_CODE` was never
reached. The captured status was discarded and the helper's own bookkeeping
became the result.

**Fix.** `|| true` on both the `kill` and the `wait`, with a comment explaining
why neither guard may be removed.

**Impact.** The one-line installer now returns the true status of
`install.sh`/`upgrade.sh`. Before this fix, chaining it with `&&`, or running it
from CI or a configuration-management tool, reported every installation as a
failure — and, worse, a genuine failure was indistinguishable from success
because both produced 143.

**Evidence.** Found during a full clean-install test in which the installation
completed correctly (service active, 0 restarts, web UI `http 200`) while the
wrapper recorded `BOOTSTRAP_EXIT_CODE=143`. A minimal reproduction under the same
shell options exits 143 without the guards and 0 with them.

---

## #254 — A fresh install ended with a hardware alarm on healthy hardware

**Symptom.** A successful clean install closed with `CONCENTRATOR MODULE NOT
DETECTED` and a GPIO, SPI and power-supply troubleshooting banner.

**Root cause.** Distinct from #252. The installation wizard writes
`wm1303_ui.json` with every channel disabled, and the backend reports its
response plainly: `NO active channels configured. Running in IDLE mode (web UI
available, radio not started)`. The radio is deliberately never started, so
`lora_pkt_fwd started`, `pktfwd ready` and `backend started` cannot appear. The
detection loop could therefore never succeed on **any** clean install and always
fell through to the hardware warning. The #252 fix does not cover this case and
makes the wait longer: 18 anchored attempts spend 90 seconds looking for markers
that will never be emitted.

**Fix.** Both scripts now test for the IDLE message before the startup markers,
so the state is recognised on the first attempt and reported as the configuration
step it is — enable at least one channel, then restart the service. The genuine
SPI and GPIO warning is preserved for the case where the radio was expected to
start and did not.

**Impact.** A new user no longer finishes their first installation being told
their concentrator is missing and sent to check SPI paths, GPIO pin numbers and
power supply, when the board is fine and only a channel is missing.

---

## Verification

All fixes were exercised by running the real upgrade end to end on a test device,
not only by inspection:

| Check | Result |
|---|---|
| `[11.4]` service status | RUNNING |
| `[11.5]` web interface | responding on port 8000, first attempt |
| `[11.6]` journal errors | no errors, phase completes |
| `[11.7]` concentrator | detected and running, first attempt |
| Final summary | reached, correct URL |
| Service after upgrade | active, 0 restarts |
| Deployed application files | md5 unchanged against pre-upgrade baseline |
| `bash -n` | passes on `install.sh`, `upgrade.sh`, `bootstrap.sh` |
| Upgrade exit code | 0 |

In addition, #253 and #254 were found and fixed during a full clean-install test:
the device was wiped completely (no unit, no package, no configuration, no data,
empty journal) and reinstalled through the published one-line installer.

| Check | Result |
|---|---|
| Wipe | verified clean, no matching file left on the filesystem |
| Repository clone | pulled from the public raw URL |
| Region and sync word | `EU868`, `private` (0x1424), taken from the environment |
| Phases | 15 phases, 0 error lines |
| HAL build | `libtools`, `libloragw`, `lora_pkt_fwd`, `spectral_scan` all built and installed |
| Overlay deploy coverage | complete |
| Service after install | active, 0 restarts, exit status 0 |
| Web interface | `http 200` |
| Configuration structure | identical key set to a reference installation |
| `[15.6]` on a fresh install | now reports the idle state instead of a hardware alarm |

## Files changed vs v2.7.5

| File | Change |
|---|---|
| `upgrade.sh` | #247, #248, #249, #251, #252, #254 |
| `install.sh` | #249, #250, #251, #252, #254 |
| `bootstrap.sh` | #253 |
| `TODO.md` | items 247 through 254 |