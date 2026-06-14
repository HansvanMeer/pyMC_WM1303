# pyMC_WM1303 v2.5.7 — Neighbours backend fixes (channel + path_len)

**Release date:** 2026-06-14
**Tag:** v2.5.7

## Overview

Two backend bug fixes that left the Neighbours tab with an empty **Channel**
column and a **Path** column showing only `0` or `≥1`. v2.5.7 records the
channel-of-arrival and the exact hop count on every advert stored in SQLite,
so the UI can finally show real per-channel and per-hop statistics.

## Bug fixes

### (1) Channel column always empty

`AdvertHelper.process_advert_packet()` never accepted a channel parameter and
never wrote one into the `advert_record` dict. As a result `last_channel` and
`channels_heard` stayed empty for every advert row.

**Fix:** overlay `handler_helpers/advert.py` adds a `channel` parameter on
`process_advert_packet()` and a `channel` key in the resulting `advert_record`.
`main.py` and `packet_router.py` pass `origin_channel` through to the helper
(`BridgeRepeaterHandler` from the WM1303 path, `PacketRouter` via the
`packet._origin_channel` attribute).

### (2) Path column lacked the exact hop count

The `adverts` table only had `zero_hop BOOLEAN`. The exact path length was
discarded, leaving the UI to show `0` or `≥1`.

**Fix:** `sqlite_handler.py` migration `adverts_path_len_v257` adds
`path_len INTEGER`. `store_advert` INSERT + UPDATE persist it; `get_neighbors`
SELECT exposes it in both the inner and outer query so the UI sees the exact
hop distance for every relayed advert that carries a path BLOB.

## Files changed

- `overlay/pymc_repeater/repeater/handler_helpers/advert.py` (new overlay)
- `overlay/pymc_repeater/repeater/data_acquisition/sqlite_handler.py`
- `overlay/pymc_repeater/repeater/main.py`
- `overlay/pymc_repeater/repeater/packet_router.py`
- `overlay/pymc_repeater/repeater/web/html/wm1303.html` (UI consumes the new columns)
- `install.sh` and `upgrade.sh` — `handler_helpers` copy loop now also copies `advert.py`
- `TODO.md` — entry #207 added
- `VERSION` bumped to `2.5.7`

## Verification (on the reference repeater)

After the service restart on first boot of v2.5.7:

- Migration `adverts_path_len_v257` applied without errors.
- First incoming ADVERT recorded with `last_channel="channel_e"`,
  `channels_heard="channel_e"`, `path_len=18`, while `zero_hop=0` stays consistent.
- Neighbours tab shows Channel and exact Path columns populated for new adverts.

## Upgrade notes

- `pymc-repeater.service` must restart to pick up the new helper. The SQLite
  migration runs automatically on the first boot of v2.5.7.
- Backward compatible: existing `adverts` rows keep `path_len = NULL` until
  they are re-heard. The UI handles NULL gracefully (falls back to `zero_hop`).
- No config changes required.
