# Topology Redesign v2 — Design Document

> **Status**: Draft — awaiting user approval before implementation
> **Author**: pyMC_WM1303 dev session
> **Date**: 2026-06-14
> **Target version**: 2.6.0 (MINOR bump — significant UI overhaul)
> **Scope**: Complete redesign of the Neighbours → Topology tab

---

## 1. Problem analysis

The current Topology tab (v2.5.8) offers 5 layout modes (`rings`, `force-hop`, `force-channel`, `mesh`, with Pad 1 + Pad 2 polish), but in real-world use with **862 neighbours** the views remain confusing:

| Issue | Observation |
|-------|-------------|
| **Visual overload** | 862 anonymous dots, no clear visual hierarchy — every node looks the same |
| **"Relayed-only" mega-cluster** | In `force-hop` view, 860 of 862 nodes collapse into a single unreadable bubble labelled "Relayed - no RSSI (860)" |
| **Star-shaped fake topology** | The `mesh` view draws all 862 edges from YOU outward → it looks like a star, not a real mesh; relay-to-relay edges (from `path` BLOB) are not yet drawn |
| **No identity** | Outside of direct-heard nodes, no labels are visible; tooltips alone require hover-by-hover exploration |
| **No quality encoding** | RSSI/SNR/recency are not consistently encoded via colour/size/opacity |
| **"All-in-one" views** | Every mode tries to show everything → no view answers a single clear question |
| **No filters that scale** | Hide-unknown / hide-relayed are binary; no continuous filters for RSSI/recency/hop-distance |

### 1.1 What good looks like (success criteria)

The redesigned Topology tab must satisfy the following — measurable on the real the test device data set (862 neighbours, ~17 direct-heard, ~845 relayed):

1. **Within 5 seconds** of opening the tab, the user can answer: *"How many neighbours can I hear directly right now, on which channels, and how strong?"*
2. **Within 10 seconds**, the user can identify the **3 strongest links** by name.
3. **Continuous filters** (sliders) for RSSI threshold, recency, hop-distance — every change re-renders within 200 ms.
4. **Stable spatial layout** — a given node sits in roughly the same place between refreshes, so the user builds a mental map.
5. **No mega-clusters** — if a group exceeds 50 nodes it must visually decompose (sub-rings, density heatmap, or paginated drill-in).
6. **Zero impact on RX/TX** — pure client-side rendering, no extra backend polling beyond what is already cached.

---

## 2. Design goals

### 2.1 Aligned with `design-principles.promptinclude.md`

- **RX priority preserved**: redesign is **frontend-only** (HTML/JS in `wm1303.html`); no backend polling changes, no extra radio activity.
- **TX efficiency preserved**: no new endpoints, no new background tasks.
- **Device-wide vs per-channel**: respected — the Topology tab is a read-only view, no configuration changes.

### 2.2 UX principles

1. **One view = one question** — each layout answers exactly one user question.
2. **Progressive disclosure** — start with summary, drill-down on demand.
3. **Visual encoding consistency** — colour = channel, size = SNR, opacity = recency (same rules across all views).
4. **Spatial stability** — a node's position is deterministic (hash-based seed) within one layout.
5. **Mobile-aware** — must remain usable on a 768 px wide tablet.

---

## 3. Target architecture

### 3.1 Libraries

| Library | Already loaded? | Purpose |
|---------|-----------------|---------|
| **vis-network 9.1.9** | ✅ Yes | Mesh-graph view (force-directed with real edges) |
| **Chart.js 4.4.0** | ✅ Yes | Quick-stats strip (sparkline, donut, bar) |
| **D3.js 7** | ❌ Add via CDN | Radar view (polar layout), sunburst, custom interactions |
| **Native `<canvas>` + SVG** | ✅ Yes | Fallback / mini-map |

**Decision**: add D3.js 7 (~85 KB gzipped) via CDN. The library is mature, license-friendly (BSD-3), and gives us polar/sunburst/force layouts without writing thousands of lines of trigonometry. No build step needed — pure CDN script tag.

### 3.2 New view structure (replaces 5 modes with 3 strong views + a stats strip)

```
┌─────────────────────────────────────────────────────────────┐
│  📊 Quick Stats Strip                                       │
│  ─────────────────────────────────────────────────────────  │
│  [Adverts/min sparkline] [Channel donut] [Hop-bar] [⟳ 3s]  │
└─────────────────────────────────────────────────────────────┘

┌─ View Switcher ─────────────────────────────────────────────┐
│  ◉ Radar    ○ Mesh Graph    ○ Activity Timeline             │
└─────────────────────────────────────────────────────────────┘

┌─ Filter Bar ────────────────────────────────────────────────┐
│  [Hop: 0/1/2+] [RSSI ≥ -120] [Seen ≤ 1h] [Channel ▼] [🔍]  │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                                                             │
│            (Active view rendering area)                     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 The three views in detail

#### View A — **Radar** (default, replaces `rings` + `force-hop`)

D3-driven polar layout:

- **YOU** = bright centre dot.
- **Concentric rings** = hop distance: `Direct (0)`, `1 hop`, `2 hops`, `3+ hops`. Each ring labelled with count.
- **Angle per node** = deterministic from `pubkey[0..1]` (16-bit hash → angle ∈ [0, 2π)), so positions are stable.
- **Node colour** = channel (channel_e orange, channel_f green, unknown grey).
- **Node size** = SNR: `clamp(8, 26, 14 + snr_dB)`.
- **Node opacity** = recency: `1.0` for ≤ 5 min, fades to `0.25` at 24 h.
- **Direct-ring spokes**: thin line YOU→node, thickness = RSSI strength.
- **"Relayed-density" ring**: outer rings show heat-map intensity rather than 845 individual dots when count > 50 in a ring (with badge `+845 relayed`).
- **Hover** = tooltip (name, RSSI, SNR, channel, last seen, hop).
- **Click** = right-side drill-down panel (reuses existing `nbShowDetail`).
- **Continuous sliders** in the filter bar update the view in real-time.

#### View B — **Mesh Graph v2** (replaces `mesh` + `force-channel`)

vis-network force-directed with **real edges**:

- Reconstruct edges from `path` BLOB (hex string in API response).
- Each hop in `path` becomes an edge between two relay candidates (with 1-byte hash disambiguation via location heuristic — already implemented in v2.5.8, retained).
- **Node size** = degree (number of distinct paths through this node).
- **Edge thickness** = how many adverts traversed that path.
- **Communities** via Louvain clustering (or channel-based fallback if Louvain too expensive client-side).
- **Highlight on hover**: light up the full path YOU → … → hovered node, dim everything else to 15 % opacity.
- **Mini-map** bottom-right for navigating large graphs.
- **Cluster collapse**: dense communities collapse into a single bubble with a count badge; click to expand.
- **Ghost nodes**: unknown hash bytes shown as small grey dots with `?`.

#### View C — **Activity Timeline** (new, replaces nothing)

Grid heatmap (rows = neighbours, cols = 10-min time buckets over last 24 h):

- Cell colour intensity = number of adverts received in that bucket.
- Rows sortable by: most-recent, most-frequent, alphabetical.
- Top 30 rows shown by default, paginated.
- Excellent for spotting *"who went silent?"* and *"who is flooding?"*.

---

## 4. Quick Stats Strip (always visible at top)

A single 80 px tall horizontal strip above the view switcher:

| Widget | Source | Visualisation |
|--------|--------|---------------|
| **Adverts/min sparkline** | `nbState.filtered` timestamps bucketed per minute over last 60 min | Chart.js line, no axes, soft fill |
| **Channel donut** | `nbState.filtered` grouped by channel | Chart.js doughnut, click slice to filter |
| **Hop-distribution bar** | count of nodes per hop level | Chart.js horizontal stacked bar |
| **Status pill** | last refresh + auto-refresh toggle | Pure CSS, no chart |

All four widgets re-render when `nbState.filtered` updates (i.e. when filters change). No new API calls.

---

## 5. Implementation phases

The redesign is split into **5 phases**, each independently testable and deployable. Every phase ends with a checkpoint where you (the user) review and approve before the next phase starts.

### Phase F1 — Quick Stats Strip *(estimated ~250 lines, low risk)*

- Add the 4-widget strip above the existing view switcher.
- Wire to existing `nbState.filtered`; reuse Chart.js (already loaded).
- **No removal of existing layouts yet** — purely additive.
- **Deliverable**: stats strip visible and live-updating on every filter change.
- **Files**: `wm1303.html` only.
- **Effort**: ~1 conversation.

### Phase F2 — Radar View (D3.js) *(estimated ~600 lines, medium risk)*

- Add D3.js 7 via CDN.
- Build the new D3 polar layout in a new function `nbTopoRenderRadarD3()`.
- Replace the existing `rings` layout option with `radar` in the layout selector.
- Keep `force` and `mesh` modes untouched.
- **Deliverable**: Radar view fully functional, stable angles, recency fade, density heat-map for outer rings.
- **Files**: `wm1303.html` only.
- **Effort**: 1–2 conversations.

### Phase F3 — Mesh Graph v2 *(estimated ~400 lines, medium risk)*

- Rewrite `nbTopoRenderMesh()` to draw **real relay-to-relay edges** from the `path` BLOB.
- Add community detection (start with channel-based, evaluate Louvain).
- Add path-highlight on hover.
- Add mini-map.
- **Deliverable**: Mesh view shows the true topology, not a star.
- **Files**: `wm1303.html` only.
- **Effort**: 1–2 conversations.

### Phase F4 — Activity Timeline & Filter Bar *(estimated ~350 lines, low risk)*

- Build the Activity Timeline heatmap (Chart.js or D3 — TBD during impl).
- Replace current toggle controls with a unified filter bar (hop, RSSI, recency, channel, search).
- All filters drive both the stats strip and the active view.
- **Deliverable**: 3 views + 1 filter bar + 1 stats strip, all consistent.
- **Files**: `wm1303.html` only.
- **Effort**: 1 conversation.

### Phase F5 — Cleanup & polish *(estimated ~200 lines removed + polish, low risk)*

- Remove the legacy `force` and `rings` code paths (replaced by Radar).
- Mobile layout testing.
- Final naming, tooltips, documentation update in `docs/ui.md`.
- Bump `VERSION` to `2.6.0`.
- **Deliverable**: clean, documented, mobile-friendly Topology tab.
- **Files**: `wm1303.html`, `docs/ui.md`, `VERSION`.
- **Effort**: 1 conversation.

---

## 6. Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| D3.js CDN unavailable on isolated networks | Low | Medium | Fallback to bundled local copy in `html/assets/` |
| 862 nodes × D3 polar render too slow | Low | High | Aggregate outer rings into heat-map at >50 nodes/ring |
| Mesh edge reconstruction creates wrong paths (1-byte hash ambiguity) | Medium | Medium | Ghost-node strategy + alternative candidates panel (already in v2.5.8) |
| Mobile layout breaks | Medium | Low | Test on 768 px viewport in F5 |
| Confusion during rollout (old vs new views) | High | Low | F2–F4 add new views alongside old; F5 removes old after approval |

---

## 7. Out of scope

- Backend changes (API endpoints, database schema)
- New radio features
- Geographic Map tab (separate task — already has its own implementation)
- Real-time WebSocket push (current polling is sufficient)

---

## 8. Approval checkpoint

Before starting Phase F1, the user must explicitly approve:

- [ ] Overall architecture (3 views + stats strip + filter bar)
- [ ] Adding D3.js 7 as a new frontend dependency
- [ ] Phased rollout approach (F1 → F5)
- [ ] Target version `2.6.0` (MINOR bump)
- [ ] No commit/push without explicit per-phase user approval

Once approved, work proceeds phase by phase, with screenshot review and user sign-off at each phase boundary.
