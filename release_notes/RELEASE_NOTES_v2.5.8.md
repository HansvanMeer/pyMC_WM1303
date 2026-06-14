# pyMC_WM1303 v2.5.8 — Topology redesign (radar + mesh) and clustering fixes

**Release date:** 2026-06-14
**Tag:** v2.5.8

## Overview

Major UI redesign of the **Neighbours → Topology** tab. vis-network 9.1.9
replaces the Chart.js bubble chart, a new D3-driven **radar** layout becomes
the default, and the **mesh** graph reconstructs real relay paths from
`packet.path` BLOBs. v2.5.8 also adds the architectural fixes that make
channel clustering work on sparse mesh data.

## Highlights

### F1 — base mesh UI integration

Replaces the legacy bubble chart with a fully interactive vis-network graph
inside `nbTopoRenderMesh`. The renderer pulls edges from `packet.path` BLOBs,
sizes nodes by degree, scales edge thickness by advert count, supports hover
path highlighting, a navigation mini-map for large graphs, cluster collapse,
and ghost nodes for unknown hash bytes.

### F2 — Radar (D3 polar) layout — new default

Replaces the legacy hop-rings layout as the default. Renders nodes on
concentric hop rings (0..7 + 8+ bucket) using D3 without vis-network.
Dispatcher `nbRenderTopology()` tears down any prior vis-network instance
when switching to radar so the SVG can take over.

### F3 — Mesh graph with relay reconstruction

`nbTopoRenderMesh` now reconstructs relay-to-relay edges from `packet.path`
BLOBs, classifies every node by `kind` (`direct`, `blob`, `inferred`),
places inferred nodes on concentric hop rings around YOU, supports per-channel
community colouring, hover path highlighting (via the new `_meshPathIndex`),
cluster collapse, and a navigation mini-map.

## F3 step-by-step fixes (this release)

### A1 — clustering excludes inferred (ring-pinned) nodes

Inferred nodes are created with `physics:false` + `fixed:true` to lock them
onto concentric hop rings. vis-network silently refuses to merge fixed
nodes — `net.cluster()` was being called on 804 such nodes and never
created a single cluster ("Bug B").

The counts loop and the `joinCondition` both now skip
`n._kind === 'inferred'`, so `net.cluster()` is either invoked correctly on
the physics-driven core or not invoked at all. No more silent fail-and-do-
nothing.

### A2 — DENSE threshold lowered (40 → 15)

After A1 the eligible-channel counts dropped (no more 804 inferred unknowns).
The DENSE drempel was reduced from 40 to 15 so `channel_e` (~20–30 real
blob-nodes) still clusters with live data; `channel_a` (≤3) stays
unclustered.

### A3 — radar honest hop helper

Two hop helpers existed with different semantics for `zero_hop=false`:
`_nbHopOf()` returned 1 (optimistic), `nbTopoHopOf()` returned null
(honest). The radar render now calls `nbTopoHopOf()`. `null` is mapped to
the outer 8+ bucket so the relayed-with-unknown-distance nodes no longer
crowd the 1-hop ring (was ~837 nodes, now 1 honest blob-1-hop node).

### A4 — manual mesh cluster renderer

vis-network's `net.cluster()` silently fails when the candidate set has no
inter-node edges (sparse path-BLOB data: all edges go YOU↔blob, never
blob↔blob). `_nbTopoMeshCluster` now hides members via
`nodes.update([{id, hidden:true}, …])` and injects a synthetic cluster-node
plus a YOU→cluster edge into the DataSets directly. Uncluster is automatic:
the toggle-off path calls `nbRenderTopology()` which rebuilds the mesh from
scratch, so the synthetic cluster-nodes disappear without an explicit
reverse pass.

## Files changed

- `overlay/pymc_repeater/repeater/web/html/wm1303.html` (~9697 lines; F2/F3 renderers, A1–A4 patches)
- `_tools/capture_radar.js` (new — radar layout capture)
- `_tools/cap_cluster_on.js` (new — cluster-on validation)
- `_tools/probe_cluster.js` (new — diagnostic for join-condition counts)
- `docs/topology-redesign-v2.md`
- `HANDOVER_v2.5.8_topology.md`
- `VERSION` is `2.5.8`

## Known behaviour at release

- **A4 deployed but headless capture timed out.** Manual browser test required
  on the dashboard URL: switch the Topology view to Mesh, then toggle
  *Cluster channel communities*. If clusters do not appear or rendering hangs,
  A4 can be reverted by restoring the pre-A4 `Object.keys` block (vis-network's
  native `net.cluster` call) — that path is safe but produces zero clusters
  on sparse data.
- **Channel-clustering depends on path-BLOB coverage.** With the current
  ~29/866 BLOB coverage on the reference unit, `channel_e` is the only
  channel above DENSE = 15 in practice.

## Upgrade notes

- Pure UI change: only `wm1303.html` ships in the overlay this release.
  `pymc-repeater.service` must restart so the new file is served.
- Backward compatible: existing data, existing API, existing config.
- No SQLite migration this release.
- Default Topology layout is now **radar**; legacy hop-rings is still
  reachable via the layout dropdown for users who prefer it.

## Verification

On the reference unit after deploy and service restart:

- Default Neighbours → Topology view is the radar (D3 polar) layout.
- Radar 1-hop ring shows the honest count (1 real blob-1-hop node);
  the outer 8+ ring holds the relayed-with-unknown-hop majority.
- Mesh view loads with the hop-ring fallback for inferred nodes; hover-
  path highlight works on real path-BLOB nodes; mini-map renders.
- Mesh `clusterNodes` is 0 by default (default-off); enabling the toggle
  triggers the manual cluster path (A4 — final visual confirmation pending
  manual browser test).
