# TX Queue & Scheduling

> Complete TX processing pipeline from RX reception to radio transmission

## Overview

Every packet transmitted by the WM1303 system passes through a multi-stage pipeline: reception, bridge evaluation, queue management, spectrum checks, and finally radio transmission. Each channel maintains its own independent `ChannelTXQueue` instance with configurable parameters for queue size, TTL, and overflow behavior.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         RADIO (SX1302 + SX1261)                     │
│  spidev0.0 (SX1302: RX/TX)    spidev0.1 (SX1261: Spectral/LBT)      │
└────────────┬───────────────────────────────┬────────────────────────┘
             │ UDP                           │ Spectral scan results
┌────────────▼───────────────────────────────▼────────────────────────┐
│              Packet Forwarder (lora_pkt_fwd)                        │
│  PUSH_DATA (RX packets)  ◄──►  PULL_RESP (TX packets)               │
└────────────┬───────────────────────────────┬────────────────────────┘
             │ UDP :1730                     ▲ UDP :1730
┌────────────▼───────────────────────────────┼────────────────────────┐
│                   WM1303 Backend                                    │
│  ┌──────────────┐  ┌─────────────────┐  ┌──────────────────────┐    │
│  │ _handle_udp()│  │NoiseFloorMonitor│  │   RX Watchdog        │    │
│  │  RX dispatch │  │  (30s interval) │  │  (3 detection modes) │    │
│  └──────┬───────┘  └────────┬────────┘  └──────────────────────┘    │
│         │                   │                                       │
│         │         ┌─────────▼──────────┐                            │
│         │         │ Spectral Scan      │                            │
│         │         │ Harvest (TX-free)  │                            │
│         │         │ → feed noise floor │                            │
│         │         │   to TX queues     │                            │
│         │         └────────────────────┘                            │
└─────────┼───────────────────────────────────────────────────────────┘
          │
┌─────────▼──────────────────────────────────────────────────────────┐
│                      Bridge Engine                                 │
│                                                                    │
│  1. RX packet received on channel_x                                │
│  2. Dedup check (has this packet been seen before?)                │
│  3. Bridge rules evaluation (which channels should receive this?)  │
│  4. Repeater handler (increment hop count, update path bytes)      │
│  5. Queue TX to all target channels                                │
│                                                                    │
└─────────┬─────────────┬──────────────┬─────────────────────────────┘
           │             │              │
    ┌──────▼──────┐ ┌───▼────────┐ ┌───▼────────┐
    │ TX Queue    │ │ TX Queue   │ │ TX Queue   │
    │ channel_a   │ │ channel_b  │ │ channel_d  │
    │ (SF8)       │ │ (SF7)      │ │ (SF7)      │
    └──────┬──────┘ └───┬────────┘ └───┬────────┘
           └────────────┴──────────────┘
                        │
                   PULL_RESP → Packet Forwarder → Radio TX
```

See [System Architecture](architecture.md) for the full system diagram and [Software Components](software.md) for details on each module.

## Phase 1 — RX Reception

| Step | Component | Action |
|------|-----------|--------|
| 1.1 | SX1302 Radio | Receives LoRa packet on one of the IF chains |
| 1.2 | Packet Forwarder | Sends PUSH_DATA (UDP) to Backend |
| 1.3 | WM1303 Backend `_handle_udp()` | Parses rxpk JSON: frequency, SF, RSSI, SNR, payload |
| 1.4 | Backend dispatch | Routes to correct VirtualLoRaRadio (channel_a/b/d) |
| 1.5 | Bridge Engine | Receives RX event on the corresponding channel |

### UDP PUSH_DATA Format (rxpk)

```json
{
  "rxpk": [{
    "freq": 869.461,
    "datr": "SF8BW125",
    "rssi": -81,
    "lsnr": 13.5,
    "size": 56,
    "data": "<base64 payload>"
  }]
}
```

See [Radio Configuration](radio.md) for details on how IF chains map to channels.

## Phase 2 — Bridge Engine Processing

| Step | Action | Detail |
|------|--------|--------|
| 2.1 | Dedup check | Hash packet and compare against recently seen packets. Prevents echo loops: if the same packet was already received (within the dedup window), it is not forwarded again. |
| 2.2 | Bridge rules evaluation | Which rules match? Each rule defines a source-channel to target-channel mapping (e.g. `channel_a → channel_b`, `channel_a → channel_d`). |
| 2.3 | Repeater handler | MeshCore-specific processing: increment hop count, update path bytes, adjust packet size. |
| 2.4 | TX batch window | Set TX hold (2s batch window). All target channels are queued simultaneously to prevent fragmentation. |
| 2.5 | Fire sends | `firing N radio sends concurrently` — parallel TX to all target channels via their individual TX queues. |

### Bridge Rules Example

Bridge rules are configured in the [WM1303 Manager UI](ui.md) and define how packets are forwarded between channels. Each rule specifies:

- Source channel (where the packet was received)
- Target channel (where it should be forwarded)
- Whether the repeater handler should process the packet

```
rule r1774374816452:  channel_a → repeater → channel_a (echo back)
rule r1774374817571:  channel_a → repeater → channel_d
rule r_rpt_to_b:      channel_a → repeater → channel_b
```

See [Configuration Reference](configuration.md) for bridge rule configuration details.

## Phase 3 — TX Queue Processing

Each channel has its own `ChannelTXQueue` instance. This is where most of the processing work happens.

### Async TX Architecture

The TX path is fully **asyncio-native**, using `asyncio.Lock` and `asyncio.sleep` instead of threading primitives. This eliminates zombie-thread deadlocks that could occur with the previous `run_in_executor` approach. Key characteristics:

- **No threading** — All TX queue processing runs within the asyncio event loop
- **Socket auto-recovery** — If the UDP socket fails, `_recreate_socket()` automatically creates a new socket and retries
- **Fair round-robin scheduling** — A rotating start index (`_round_index`) ensures equal TX priority across channels, preventing any single channel from monopolizing the TX path
- **Dynamic TX hold** — Batch window hold scales with queue depth: 0 pending = 0 ms, 1 pending = 100 ms, 2+ pending = 0 ms (immediate processing)

### Flow Diagram

```
┌─────────────────────────────────────────────────────────┐
│                  ChannelTXQueue                         │
│                                                         │
│  3.1  PACKET ARRIVAL                                    │
│       ↓                                                 │
│  3.2  QUEUE CHECK                                       │
│       Queue full (>15)?                                 │
│       → YES: drop OLDEST packet (dropped_overflow +1)   │
│       → NO:  add packet to queue                        │
│       ↓                                                 │
│  3.3  TTL CHECK                                         │
│       Packet older than 5 sec?                          │
│       → YES: drop (dropped_ttl +1)                      │
│       → NO:  proceed to TX preparation                  │
│       ↓                                                 │
│  3.4  TX HOLD CHECK                                     │
│       Is a batch window hold active?                     │
│       (batch window 2s only — noise floor hold removed)  │
│       → YES: wait until hold expires                    │
│       → NO:  proceed                                    │
│       ↓                                                 │
│  3.5  LBT CHECK (if enabled per channel)                │
│       Retrieve noise floor from rolling buffer          │
│       Compare RSSI against adaptive threshold           │
│       → RSSI > threshold: blocked (lbt_blocked +1)      │
│       → RSSI ≤ threshold: channel clear (lbt_passed +1) │
│       ↓                                                 │
│  3.6  CAD CHECK (if enabled per channel)                │
│       HW CAD (SX1261 preamble correlation) + SW CAD      │
│       → LoRa activity detected: wait + retry             │
│       → No activity: channel clear (cad_clear +1)        │
│       → Source tracked: cad_hw_* / cad_sw_* counters     │
│       ↓                                                 │
│  3.7  TX EXECUTE                                        │
│       PULL_RESP to packet forwarder (UDP)               │
│       Contains: freq, SF, BW, CR, power, payload        │
│       ↓                                                 │
│  3.8  TX CONFIRMATION                                   │
│       Wait for TX_ACK from packet forwarder             │
│       → Success: total_sent +1                          │
│       → Failure: total_failed +1                        │
│       ↓                                                 │
│  3.9  UPDATE STATISTICS                                 │
│       send_ms, airtime_ms, wait_ms, lbt_last_rssi       │
│       noise_floor rolling buffer update                 │
└─────────────────────────────────────────────────────────┘
```

See [LBT & CAD](lbt_cad.md) for detailed documentation on steps 3.5 and 3.6.

### Queue Parameters

| Parameter | Value | Source | Description |
|-----------|-------|--------|-------------|
| Max queue size | 15 | `wm1303_ui.json` → `adv_config.max_cache_size` | Per channel. Prevents memory exhaustion under heavy load |
| TTL (time-to-live) | 5 sec | `wm1303_ui.json` → `adv_config.tx_packet_ttl_seconds` | Packets older than TTL are automatically dropped |
| Overflow policy | drop oldest | `wm1303_ui.json` → `adv_config.tx_overflow_policy` | When queue is full, the oldest packet is removed |
| Processing order | FIFO | hardcoded | First In, First Out — oldest packets are processed first |

### Batch Window

When the Bridge Engine forwards a packet to multiple channels simultaneously, a **2-second batch window** is used:

1. Bridge Engine evaluates all matching rules for an incoming packet
2. A 2-second TX hold is set to collect all target channels
3. After the batch window, all queued packets are sent concurrently
4. This prevents fragmentation — all channels get the forwarded packet within the same time window

### Inter-Packet Gap

When multiple packets are queued for sequential transmission, a **50 ms inter-packet gap** is maintained between TX operations. This allows:

- The SX1302 to complete RF switch transitions
- AGC recalibration between transmissions
- SPI bus settling time

## Phase 4 — Radio TX

The SX1302 is **half-duplex** — RX is interrupted during the entire TX cycle.

| Step | Duration | What Happens |
|------|----------|-------------|
| 4.1 | ~0.1 ms | RF switch → TX mode (RX stops) |
| 4.2 | 65-230 ms | Packet transmission (depends on SF + payload size) |
| 4.3 | ~0.1 ms | RF switch → RX mode |
| 4.4 | 30-50 ms | AGC recalibration (automatic) |
| 4.5 | — | RX resumes |

### TX Airtime per Channel

Typical MeshCore packet ~56-80 bytes:

| Channel | SF | BW | 60 bytes | 80 bytes |
|---------|----|----|----------|----------|
| ch-a | SF8 | 125 kHz | ~120 ms | ~150 ms |
| ch-b | SF7 | 125 kHz | ~65 ms | ~80 ms |
| ch-d | SF7 | 125 kHz | ~65 ms | ~80 ms |

### TX Throughput per Channel (including overhead)

| Channel | LBT | Airtime | AGC Reload | Total |
|---------|-----|---------|------------|-------|
| ch-a (SF8, no LBT) | 0 ms | ~120 ms | ~40 ms | ~160 ms |
| ch-b (SF7, with LBT+CAD) | ~5 ms | ~65 ms | ~40 ms | ~110 ms |
| ch-d (SF7, with LBT+CAD) | ~5 ms | ~65 ms | ~40 ms | ~110 ms |

### RX Interruption

The SX1302 is half-duplex on the same RF chain. RX is interrupted during the full TX cycle:

```
RX active ────┐
              ├── RF switch to TX       (~0.1 ms)
              ├── TX airtime            (65-150 ms)
              ├── RF switch to RX       (~0.1 ms)
              ├── AGC reload/recal      (30-50 ms)
RX resumes ───┘
```

| Channel | RX Blind Window per TX |
|---------|-----------------------|
| ch-a (SF8) | ~160-200 ms |
| ch-b (SF7) | ~100-120 ms |
| ch-d (SF7) | ~100-120 ms |

### Sequential Bridge Forwarding (3 channels)

Packets are sent **sequentially** — the SX1302 can only transmit one packet at a time:

```
├─ TX ch-A (SF8): ~160 ms  ──► RX blind
├─ TX ch-B (SF7): ~110 ms  ──► RX blind
├─ TX ch-D (SF7): ~110 ms  ──► RX blind
└─ Total RX interruption: ~380 ms
```

With a typical message every ~12 seconds:
- 380 ms / 12,000 ms = ~3.2% RX loss

See [Radio Configuration](radio.md) for RF chain details and [Hardware Overview](hardware.md) for the SX1302 half-duplex architecture.

## Echo Prevention

The system implements self-echo prevention using packet hashing:

1. When a packet is received (RX), a hash is computed from the payload
2. Before forwarding via bridge rules, the hash is checked against recently transmitted packets
3. If the hash matches a recently sent packet, it is recognized as an echo and dropped
4. The dedup TTL (default: 300 seconds) controls how long hashes are retained

This prevents infinite forwarding loops where a bridged packet is received back on the original channel and forwarded again.

## Noise Floor Scan & TX Queue Interaction

Every 30 seconds, the `NoiseFloorMonitor` harvests spectral scan data:

1. The monitor checks whether any TX is currently in progress
2. If TX is active, the harvest is retried after a short delay (waiting for a TX-free window)
3. The mutex is released during retry, preserving RX availability
4. When a TX-free window is found, noise floor RSSI values are harvested into per-channel rolling buffers
5. TX queues are **never paused** — this results in **0% TX overhead** from noise floor monitoring

See [LBT & CAD](lbt_cad.md) for full details on noise floor measurement and the retry mechanism.

## SPI Bus Timing

The system uses two SPI buses operating at 2 MHz:

| Device | SPI Path | Clock | Function |
|--------|----------|-------|----------|
| SX1302 | `/dev/spidev0.0` | 2 MHz | Main concentrator (RX + TX) |
| SX1261 | `/dev/spidev0.1` | 2 MHz | Spectral scan, LBT, CAD |

### SPI Bus Load Analysis

| Activity | SPI Time per Cycle | Frequency | Bus Load |
|----------|-------------------|-----------|----------|
| RX polling | ~1 ms | 10x/sec | ~1% |
| TX transmit | ~2 ms | incidental | <0.5% |
| Spectral scan | ~5 ms per freq step | 1x per 30s sweep | ~5-10% |
| **Total** | | | **< 15%** |

At 2 MHz, the SPI bus has a theoretical throughput of 250 KB/s. There is ample bandwidth for all operations. The two SPI buses operate independently, so SX1261 spectral scanning does not compete with SX1302 RX/TX operations.

See [Hardware Overview](hardware.md) for the physical SPI bus architecture.

## TX Queue Statistics (API)

### Endpoint: `/api/wm1303/tx_queues`

Returns per-channel TX queue statistics:

| Category | Field | Description |
|----------|-------|-------------|
| Queue | `pending` | Packets currently in queue |
| Queue | `total_sent` | Successfully transmitted packets |
| Queue | `total_failed` | Failed TX attempts |
| Queue | `dropped_overflow` | Dropped due to full queue |
| Queue | `dropped_ttl` | Dropped due to expired TTL (>5s) |
| Timing | `avg_airtime_ms` | Average over-the-air transmission time |
| Timing | `avg_send_ms` | Average SPI write time |
| Timing | `avg_wait_ms` | Average time waiting in queue |
| Timing | `last_airtime_ms` | Airtime of the most recent packet |
| LBT | `lbt_passed` | TX allowed by LBT check |
| LBT | `lbt_blocked` | TX blocked by LBT check |
| LBT | `lbt_skipped` | TX where LBT was skipped (disabled) |
| LBT | `lbt_last_rssi` | Last measured RSSI value |
| CAD | `cad_clear` | Channel was clear at CAD check (combined total) |
| CAD | `cad_detected` | LoRa activity detected at CAD check (combined total) |
| CAD | `cad_timeout` | CAD check timed out |
| CAD | `cad_hw_clear` | Hardware CAD (SX1261 preamble) reported channel clear |
| CAD | `cad_hw_detected` | Hardware CAD detected LoRa preamble |
| CAD | `cad_sw_clear` | Software CAD (spectral analysis) reported channel clear |
| CAD | `cad_sw_detected` | Software CAD detected LoRa activity |
| CAD | `cad_last_source` | Source of last CAD result: `"hw"` or `"sw"` |
| Noise | `noise_floor_lbt_avg` | Average noise floor (20 samples) |
| Noise | `noise_floor_lbt_min` | Minimum noise floor |
| Noise | `noise_floor_lbt_max` | Maximum noise floor |
| Noise | `noise_floor_lbt_samples` | Number of samples in buffer |
| Duty | `tx_duty_pct` | Current duty cycle percentage |

### Endpoint: `/api/wm1303/channels/live`

Returns per-channel live data including noise floor and LBT statistics:

| Field | Description |
|-------|-------------|
| `noise_floor` | Current noise floor (from LBT RSSI buffer or fallback) |
| `noise_floor_lbt_avg` | Average noise floor from RSSI measurements |
| `noise_floor_lbt_min` | Minimum noise floor |
| `noise_floor_lbt_max` | Maximum noise floor |
| `noise_floor_lbt_samples` | Number of measurements in buffer |
| `rssi_last` | RSSI of last received packet |
| `rssi_avg` | Average RSSI over all received packets |
| `lbt_blocked` | Times TX was blocked |
| `lbt_passed` | Times TX was allowed |
| `lbt_last_rssi` | RSSI at last LBT measurement |

See [API Reference](api.md) for all available endpoints.

## Background Processes

### RX Watchdog

The RX Watchdog continuously monitors radio health using three detection methods:

| Detection Method | Trigger | Action |
|-----------------|---------|--------|
| PUSH_DATA statistics | 2x `rxnb=0` while TX active (~60s) | Restart packet forwarder |
| RSSI spike detection | 5+ strong signals but no successful RX (~60s) | Restart packet forwarder |
| RX timeout | No RX packet for 180 seconds | Restart packet forwarder |

The watchdog ensures the system self-recovers from radio lockups, which can occur after TX-induced SX1250 desensitization. See [Hardware Overview](hardware.md) for details on the FEM/PA issue.

## Troubleshooting

### TX Queue Full (queue_full errors)

**Symptoms:** Nodes do not receive ACK, journal shows `queue full, dropping packet`.

**Solutions:**
1. Check `pending` value via `/api/wm1303/tx_queues`
2. If pending > 10: restart service (`sudo systemctl restart pymc-repeater`)
3. Verify TTL and max queue size are correctly configured
4. Check if batch window hold is stuck (bridge engine issue)

### Packets Never Transmitted

**Symptoms:** `total_sent` stays at 0, queue fills up.

**Possible causes:**
- LBT blocking all transmissions (check `lbt_blocked` counter)
- Batch window hold stuck (check journal for bridge engine errors)
- Packet forwarder not running (check `systemctl status pymc-repeater`)
- SPI bus error (check `dmesg | grep spi`)

### High `dropped_ttl` Count

**Symptoms:** Many packets are dropped due to TTL expiration.

**Cause:** Packets are waiting too long in the queue before being processed. This can happen when LBT/CAD blocks are frequent or batch window holds accumulate.

**Solutions:**
1. Increase TTL if appropriate (in `wm1303_ui.json` → `adv_config.tx_packet_ttl_seconds`)
2. Check noise floor scan interval — 30s may be too frequent for heavy traffic
3. Reduce the number of active channels to decrease TX contention

### High `dropped_overflow` Count

**Symptoms:** Queue is frequently full, packets are dropped.

**Cause:** More packets arriving than can be transmitted. Common when many bridge rules forward to the same channel.

**Solutions:**
1. Review bridge rules — reduce unnecessary forwarding
2. Check if TX is actually working (not stuck)
3. Consider reducing the number of channels or bridge rules

## Configuration Files

| File | Location | Relevant Settings |
|------|----------|-------------------|
| `config.yaml` | `/etc/pymc_repeater/` | Bridge rules, dedup TTL, duty cycle, TX delay factors |
| `wm1303_ui.json` | `/etc/pymc_repeater/` | Per-channel LBT/CAD enable, queue parameters, batch settings |
| `global_conf.json` | `/home/pi/wm1303_pf/` | HAL-level LBT (disabled), spectral scan configuration |

See [Configuration Reference](configuration.md) for complete documentation of all settings.

## Related Documentation

- [LBT & CAD](lbt_cad.md) — Detailed Listen Before Talk and Channel Activity Detection documentation
- [System Architecture](architecture.md) — Full system overview
- [Radio Configuration](radio.md) — RF chains, IF chains, and SPI bus details
- [Software Components](software.md) — Backend, bridge engine, and TX queue implementation
- [API Reference](api.md) — REST API endpoints for TX queue statistics
- [Configuration Reference](configuration.md) — All configuration file parameters
