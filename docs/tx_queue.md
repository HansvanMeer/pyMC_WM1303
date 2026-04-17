# TX Queue System

> Per-channel TX queue architecture, scheduling, and hold behavior

## Overview

The WM1303 system uses **per-channel TX queues** managed by a `GlobalTXScheduler`. Each active channel (A–E) has its own queue instance that handles buffering, gating, and fair transmission scheduling.

## Architecture

```
Bridge Engine → TX batch window (2s)
    → Per-channel TX Queue instances
        ├── Channel A Queue
        ├── Channel B Queue
        ├── Channel C Queue
        ├── Channel D Queue
        └── Channel E Queue
    → GlobalTXScheduler
        → Fair round-robin scheduling
        → Per-queue gating (LBT, CAD, TTL, overflow)
        → PULL_RESP (UDP :1730) → Packet Forwarder → Radio TX
```

## Queue Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| Max queue depth | 15 packets | Per channel |
| TTL per packet | 5 seconds | Packet expires if not sent within this time |
| TX batch window | 2 seconds | Group bridge sends for concurrent queuing |
| Queue depth hold | 100ms | Brief dedup window when 1 packet pending |

## Scheduling

### Fair Round-Robin

The `GlobalTXScheduler` uses a **rotating start index** to ensure fair access across channels:

1. On each scheduling cycle, iterate through all channel queues
2. Start from a different channel each cycle (rotating index)
3. For each queue, check if the head packet can be sent (gating checks)
4. If yes, dequeue and send via PULL_RESP
5. Track TX statistics per channel

This prevents one busy channel from starving others.

### Gating Checks (Per-Packet)

Before each TX, the queue performs these checks in order:

| # | Check | Action on Fail |
|---|-------|----------------|
| 1 | **TTL** | Packet expired → discard |
| 2 | **Queue overflow** | Queue full → discard oldest |
| 3 | **LBT** (if enabled) | Channel busy → retry later |
| 4 | **CAD** (if LBT+CAD enabled) | LoRa activity detected → retry later |

## TX Batch Window

When the Bridge Engine forwards a packet to multiple target channels, it uses a **2-second batch window**:

1. First target channel enqueue triggers the batch timer
2. Additional target channels are enqueued within the window
3. After 2 seconds, all queued packets are eligible for scheduling
4. This groups related forwards for efficient multi-channel TX

## TX Hold History

The TX hold model has evolved:

| Version | Hold Type | Duration | Status |
|---------|-----------|----------|--------|
| Early | Noise floor hold | ~4 seconds | ❌ **Removed** |
| Early | TX batch window | 2 seconds | ✅ Active |
| Current | LBT/CAD gating | Variable | ✅ Active (per-channel) |
| Current | Queue depth hold | 100ms | ✅ Active |

The noise floor hold was removed because it violated the "TX ASAP" design principle and was unnecessary — the SX1261 spectral scan runs on a separate SPI bus.

## Noise Floor Integration

Each TX queue receives per-channel noise floor values from the NoiseFloorMonitor:

- Values stored in a rolling buffer (20 samples)
- Used for LBT threshold comparison
- Updated every 30 seconds (monitor interval)
- **Does NOT pause the queue** — values are fed asynchronously

## TX Statistics

Per-channel TX statistics tracked:

| Metric | Description |
|--------|-------------|
| `tx_sent` | Packets successfully sent |
| `tx_dropped_ttl` | Packets expired before send |
| `tx_dropped_overflow` | Packets dropped due to full queue |
| `tx_lbt_blocked` | TX attempts blocked by LBT |
| `tx_cad_blocked` | TX attempts blocked by CAD |
| `tx_duty_cycle` | Cumulative TX duty cycle (sum across channels sharing an RF chain) |

### Duty Cycle Calculation

Since Channels A–D share the SX1250 RF chain, the TX duty cycle is the **sum** of all individual channel duty cycles (fixed in v2.0.0 — was previously averaged).

## Socket Recovery

The TX path includes automatic socket recovery:

- If UDP send fails, the socket is recreated
- Retry logic ensures packets are not lost on transient failures
- Socket errors are logged for diagnostics

## Channel E TX

Channel E TX follows the same queue model but uses the SX1261 radio path:

- Supports sub-125 kHz bandwidths (62.5 kHz)
- TX power configurable per channel
- LBT/CAD via SX1261 direct measurement

## Design Principles

1. **TX ASAP** — Minimize delay between enqueue and actual transmission
2. **RX priority** — Never block RX for TX operations
3. **Fairness** — No channel should starve others
4. **Safety** — TTL and overflow prevent unbounded queue growth
5. **Compliance** — LBT/CAD support EU regulatory requirements

## Related Documents

- [`lbt_cad.md`](./lbt_cad.md) — LBT and CAD behavior
- [`radio.md`](./radio.md) — Radio architecture
- [`architecture.md`](./architecture.md) — System architecture
- [`software.md`](./software.md) — Software components
