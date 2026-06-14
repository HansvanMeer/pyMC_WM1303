# v2.5.9 — Hop-bucket count pills in Radar view

**Release date**: 2026-06-15  
**Type**: Patch (UI enhancement, no backend changes)

## Summary

Adds a live hop-bucket count legend below the topology stats strip in the Radar layout. Each non-empty hop ring (Direct through 8+) gets a pill badge showing how many nodes fall into that bucket.

## Why

After v2.5.8 introduced the 9-ring Radar with the honest `nbTopoHopOf` helper (A3), users could see WHERE nodes cluster but had no quick numeric overview. Hovering each node to count manually is impractical with ~800 nodes in the 8+ bucket.

## Change

In `nbTopoRenderRadarD3()` (file `overlay/pymc_repeater/repeater/web/html/wm1303.html`), just after the `svg.on('mouseleave', hideTip)` line, ~20 lines were added that:
- Reuse the local `rings` object already built earlier in the function.
- Inject a `<div id="nbTopoRadarHopStats">` after `#nbTopoStats` (created lazily, once).
- Render one pill-styled `<span>` per non-empty ring with the bucket label and count.

Pills use an indigo tint matching the topology theme with rounded borders; they re-render on every radar redraw cycle.

## Files changed

- `overlay/pymc_repeater/repeater/web/html/wm1303.html` (+20 lines)
- `VERSION` (2.5.8 -> 2.5.9)

## Verification

1. Open the WM1303 web UI.
2. Navigate to Neighbours -> Topology (Radar is the default).
3. Below the existing stats strip, pill badges appear with live counts, e.g. `Direct: 1 / 1 hop: 1 / ... / 8+ hops: 801`.
4. Empty rings are not shown.

## Upgrade

Run `./upgrade.sh` on the repeater. No database migration required.

## Known issues

None at this time.
