# pyMC_WM1303 v2.7.3 — Release Notes

**Release date:** 2026-08-11
**Type:** Patch (bug fixes + Layer-2 hardening + operator diagnostics + missing UI endpoints, no breaking changes)
**Upgrade:** Safe drop-in over v2.7.2 via the standard bootstrap one-liner.

## Summary

Six related improvements around Layer-2 protocol validation, operator diagnostics and upstream-UI compatibility: (1) the Layer-2 protocol validator is now also wired into every non-RF ingress path (MQTT, companion, repeater re-inject); (2) a new spec-strictness check catches path-length violations that upstream `Packet.cpp isValidPathLen()` rejects; (3) the 'Invalid Packets' UI tab now shows an explicit decode-reliability indicator per row and colour-codes the sender-hint so operators immediately see which fields are trustworthy; (4) two long-standing bugs fixed (ADVERT-record JSON-serialisation crash, `invalid_packets` missing from retention); (5) the Analytics > Neighbour Links tab is restored by adding the two missing `/api/neighbor_links` and `/api/neighbor_link_history` endpoints the newer upstream Vue-UI expects.

## Fixes and improvements

### #218 — Layer-2 validator ingress-path: MQTT/companion/repeater re-inject now also validated

- **Symptom / gap:** the central Layer-2 protocol validator was only wired into the RF RX-callback (`wm1303_backend._process_rx_packet`), so any packet arriving through `BridgeEngine.inject_packet()` — MQTT, companion frame server, channel_e/f, repeater re-inject — bypassed it entirely and could reach the bridge/MQTT-forward/dispatcher unchecked.
- **Fix:** added the same `protocol_validator.validate_and_record(...)` block at the top of `BridgeEngine.inject_packet()` in `overlay/pymc_repeater/repeater/bridge_engine.py`, right after the `_running` guard and before echo/dedup/inject/MQTT-forward. Fire-and-forget try/except so validator errors never block the injection path (RX-availability #1 design principle).
- **Impact:** malformed frames from every ingress source are now blocked before reaching downstream consumers — the RF path and non-RF paths behave identically w.r.t. Layer-2 filtering.

### #219 — Layer-2 validator spec-strictness: new `path_bytes_exceed_max` drop_reason (upstream `isValidPathLen()` parity)

- **Symptom / gap:** the validator missed the explicit `hash_count × hash_size > MAX_PATH_SIZE (64)` check that upstream `Packet.cpp isValidPathLen()` enforces. In large enough frames a declared path total of >64 bytes (e.g. `hash_size=2 × hop_count=40 = 80 bytes`) could slip past `length_implausible` even though upstream would reject it as structurally invalid.
- **Fix:** added check 7c in `overlay/pymc_repeater/repeater/protocol_validator.py` (between `hop_count_implausible` and `length_implausible`): `if hop_count * hash_size > MAX_PATH_SIZE: return _fail("path_bytes_exceed_max", metadata)`. Docstring drop_reasons list updated. UI updates in `overlay/pymc_repeater/repeater/web/html/wm1303.html`: new `REASON_INFO` + `REASON_DETAIL` entries with byte-arithmetic explanation, `RELIABILITY = 'partial'`. `length_implausible` and `path_overflow` tips also sharpened for accuracy.
- **Impact:** restores full spec-parity with upstream `Packet.cpp isValidPathLen()`; no structurally-invalid frame can slip past the validator regardless of frame size. Live on the test device: **184 rows** with the new drop_reason recorded within 10 minutes, capturing real garbage-frames that previously slipped through.

### #220 — 'Invalid Packets' UI — decode-reliability indicator per row + colour-coded sender-hint

- **Symptom / gap:** the forensic detail view (added in v2.7.2, item #217) showed decoded fields per row but did not make explicit which fields are actually trustworthy per `drop_reason`. Operators had to memorise per-`drop_reason` semantics to know that e.g. path/hint are noise when the structure itself is corrupt (`reserved_path_len_hash_size_4`) versus when the header decode is fine but the frame simply has an unsupported payload version.
- **Fix:** added a per-`drop_reason` `RELIABILITY` map in `overlay/pymc_repeater/repeater/web/html/wm1303.html` with three levels: 🟢 **Reliable** (header-only drops: `invalid_route_type`, `unsupported_payload_version`, `unknown_payload_type`, `payload_too_short_for_type`), 🟡 **Partial** (`path_overflow`, `hop_count_implausible`, `path_bytes_exceed_max`, or 🟢 auto-downgraded when sender-hint = `unknown-*` fallback), 🔴 **Untrusted / corrupt** (`reserved_path_len_hash_size_4`, `too_short`, `length_implausible`, `transport_code_length_mismatch`). Row-level UI: coloured dot next to the caret. Sender-hint column: green for a real hex first-hop hash, red for the `unknown-<sha>` fallback. Detail-view UI: big badge on top, border-left colour follows the level, explicit `⚠️ do not trust — raw bytes only` warnings on Path hex + Sender hint at 🔴, and Raw packet marked green with `(always the truth)`.
- **Impact:** operators immediately see which decoded fields to trust per row without memorising per-`drop_reason` semantics — makes diagnosis of spammer/garbage vs real protocol violations far faster and less error-prone.

### #221 — Bug: `AdvertHelper: Failed to store advert record: Object of type bytes is not JSON serializable`

- **Symptom:** every received ADVERT (~10/hour on a busy device) failed to persist to the `adverts` table with a JSON-serialisation error. The advert was still forwarded to the mesh and neighbour tracking still worked, but the DB row was missing — breaking advert statistics, UI history and neighbour analytics.
- **Root cause:** `advert_record["path"] = path_bytes_blob` in `overlay/pymc_repeater/repeater/handler_helpers/advert.py` passed a raw `bytes` object into a dict that gets JSON-encoded downstream (`storage.record_advert` → `json.dumps`), which cannot serialise `bytes`.
- **Fix:** defensively convert to hex string in the record: `"path": (path_bytes_blob.hex() if isinstance(path_bytes_blob, (bytes, bytearray)) else (path_bytes_blob or ""))`. Verified on the test device: **0** `Failed to store advert record` errors in the journal since deploy (was recurring ~10×/hour before).
- **Impact:** restores per-ADVERT database persistence on every device — the `adverts` table now grows correctly with every incoming advert.

### #222 — Bug: `invalid_packets` missing from `metrics_retention` (unbounded growth past 8-day policy)

- **Symptom:** the `invalid_packets` table had rows older than 12 days on the test device while other retention-managed tables (`packets`, `adverts`, `crc_errors`) were correctly pruned at 8 days. The design-doc retention policy was silently not enforced for this table.
- **Root cause:** `overlay/pymc_repeater/repeater/metrics_retention.py` `DELETE_ONLY_TABLES` list did not include `invalid_packets` — only `packets`, `adverts`, `crc_errors`, `noise_floor`, `sx1261_health_events`, `spectrum_scans`.
- **Fix:** added `("repeater.db", "invalid_packets", "timestamp")` between `crc_errors` and `noise_floor` in the list, with an explanatory comment referencing the design-doc requirement. Verified on the test device: runtime-inspect confirms `invalid_packets in DELETE_ONLY_TABLES: True`; next hourly cleanup will start pruning the backlog to the 8-day cutoff.
- **Impact:** restores the design-doc 8-day retention policy for the `invalid_packets` table on every device; prevents unbounded table growth (~1000+ rows/day) that would eventually inflate DB size.

### #223 — Analytics > Neighbour Links tab 'Loading Failed' — missing `/api/neighbor_links` + `/api/neighbor_link_history` endpoints (#208-pattern)

- **Symptom:** the Analytics > Neighbour Links tab (with EWMA RX Score, Live Links, Duplicate Observation Ratio, per-peer RSSI/SNR history charts) showed 'Loading Failed / Unknown error occurred / Retry' on every load.
- **Root cause:** exact #208-pattern. The Vue-client (`repeater/web/html/assets/api-sB-WuUnO.js`) calls `GET /api/neighbor_links` and `GET /api/neighbor_link_history` (US-spelling), but the HansvanMeer-fork backend only had British `_neighbours_*` helper methods mounted at `/api/wm1303/*`, not `/api/*`. Both new endpoints returned HTTP 404 (verified with a valid JWT: real CherryPy 404 Not Found HTML, not the SPA catch-all) → the axios-client threw → the UI toggled into the error state.
- **Fix:** added two new `@cherrypy.expose` `@cherrypy.tools.json_out()` endpoints to `APIEndpoints` in `overlay/pymc_repeater/repeater/web/api_endpoints.py`: `neighbor_links(**params)` and `neighbor_link_history(**params)`. Both match the exact response envelope the Vue-UI expects: `{success:true, data:{...}}` with 12 per-link keys (`peer_hash / friendly_name / path_hash_size / path_hop_count / rssi / snr / sample_count / duplicate_sample_count / is_duplicate / active / ewma_score / last_seen`) and 5 summary keys (`links[] / total_links / active_links / avg_ewma_rx_score / duplicate_observation_ratio / generated_at`). Data source: existing `storage.get_neighbors()` + `storage.get_neighbour_samples()`. Best-effort fields (explicitly documented in the code): `ewma_score` = SNR-normalised 0.6× + RSSI-normalised 0.4× (no persistent EWMA in the fork yet); `path_hash_size = 1` (MeshCore default); `duplicate_sample_count = 0` (SELECT does not surface duplicate_count yet — follow-up item). Empty data-source correctly returns valid empty JSON so the UI shows "No neighbour links observed yet" instead of 'Loading Failed'. No install.sh/upgrade.sh changes — `api_endpoints.py` is already in both copy-loops.
- **Impact:** restores the Analytics > Neighbour Links tab on every device. Same class of upstream-UI-vs-fork-backend gap as v2.7.2's #208 (Observer Templates dropdown, site_info, packet_by_hash, packet_by_id, policy_groups, policy_group_entries).

## Verification

All fixes deployed to the test device (the test device) running v2.7.2 and verified: service `active (running)`, `NRestarts=0`, no new tracebacks after restart.

- **#218**: runtime-inspect of `BridgeEngine.inject_packet` source contains `protocol_validator import validate_and_record`.
- **#219**: runtime `validate()` source contains `path_bytes_exceed_max`; UI HTTP 200 with 3 marker hits (INFO/DETAIL/RELIABILITY); **184 rows** with the new drop_reason recorded within 10 minutes of deploy.
- **#220**: JS `node --check` OK, 12 marker hits in deployed HTML, curl HTTP 200 with 10 hits.
- **#221**: **0** `Failed to store advert record` errors in the journal since deploy (was recurring ~10×/hour before).
- **#222**: runtime-inspect confirms `invalid_packets in DELETE_ONLY_TABLES: True [('repeater.db', 'invalid_packets', 'timestamp')]`.
- **#223**: `GET /api/neighbor_links?limit=500` returns **HTTP 200 application/json** with 500 links in the correct envelope shape; `GET /api/neighbor_link_history` returns valid JSON (empty for peers without samples yet); no 404s, no HTML fallback.

## Upgrade

Standard bootstrap one-liner from README/docs. No manual steps required. Configuration files in `/etc/openhop_repeater/` are preserved.

## Files changed vs v2.7.2

- `overlay/pymc_repeater/repeater/bridge_engine.py` (#218)
- `overlay/pymc_repeater/repeater/protocol_validator.py` (#219)
- `overlay/pymc_repeater/repeater/handler_helpers/advert.py` (#221)
- `overlay/pymc_repeater/repeater/metrics_retention.py` (#222)
- `overlay/pymc_repeater/repeater/web/api_endpoints.py` (#223)
- `overlay/pymc_repeater/repeater/web/html/wm1303.html` (#219 + #220)
- `VERSION` (2.7.2 → 2.7.3)
- `TODO.md`
- `release_notes/RELEASE_NOTES_v2.7.3.md` (new)
