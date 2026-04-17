# Repositories

> Repository structure, overlay strategy, and upstream relationships

## Repository Overview

The WM1303 system is built from **four repositories**. Three are forks of upstream projects; one (this repo) contains the integration layer.

| Repository | Type | Branch | Purpose |
|-----------|------|--------|---------|
| [HansvanMeer/pyMC_WM1303](https://github.com/HansvanMeer/pyMC_WM1303) | **This repo** | `main` | Installation, overlays, config, docs, scripts |
| [HansvanMeer/sx1302_hal](https://github.com/HansvanMeer/sx1302_hal) | Fork | default | SX1302 HAL v2.10 — C library + packet forwarder |
| [HansvanMeer/pyMC_core](https://github.com/HansvanMeer/pyMC_core) | Fork | `dev` | MeshCore core Python library |
| [HansvanMeer/pyMC_Repeater](https://github.com/HansvanMeer/pyMC_Repeater) | Fork | `dev` | MeshCore repeater application |

### Important Rule

**The fork repositories are not modified directly.** All WM1303-specific changes are applied as overlay files from this repository (pyMC_WM1303). The forks are kept in sync with their upstream sources.

## Overlay Strategy

The overlay strategy avoids modifying fork repositories while adding WM1303-specific functionality:

```
pyMC_WM1303/overlay/
├── hal/                    → copied into sx1302_hal/
│   ├── libloragw/src/      → HAL C source overlays
│   ├── libloragw/inc/      → HAL C header overlays
│   ├── libloragw/Makefile  → Modified Makefile
│   ├── packet_forwarder/src/ → Packet forwarder overlays
│   ├── packet_forwarder/inc/ → Packet forwarder headers
│   └── packet_forwarder/Makefile → Modified Makefile
│
├── pymc_core/              → copied into pyMC_core/
│   └── src/pymc_core/hardware/
│       ├── wm1303_backend.py    → WM1303 concentrator backend
│       ├── virtual_radio.py     → VirtualLoRaRadio per-channel abstraction
│       ├── tx_queue.py          → Per-channel TX queue
│       ├── sx1261_driver.py     → SX1261 companion radio driver
│       └── sx1302_hal.py        → HAL wrapper
│
└── pymc_repeater/          → copied into pyMC_Repeater/
    └── repeater/
        ├── main.py              → Modified main (bridge init, SSOT loading)
        ├── bridge_engine.py     → Cross-channel packet routing
        ├── channel_e_bridge.py  → Channel E integration
        ├── engine.py            → Modified repeater engine
        ├── config.py            → Modified config (radio_type: wm1303)
        ├── config_manager.py    → Configuration management
        ├── identity_manager.py  → Device identity
        ├── packet_router.py     → Packet routing
        ├── data_acquisition/
        │   ├── sqlite_handler.py → Modified DB (dedup_events table)
        │   └── storage_collector.py → Data collection
        └── web/
            ├── wm1303_api.py       → WM1303 REST API
            ├── http_server.py      → Modified HTTP server (mount WM1303 API)
            ├── api_endpoints.py    → Modified API (WM1303 hardware option)
            ├── spectrum_collector.py → Spectral scan data collection
            ├── cad_calibration_engine.py → CAD calibration
            └── html/
                └── wm1303.html     → WM1303 Manager UI
```

## HAL Overlay Details

The HAL overlay modifies the Semtech SX1302 HAL v2.10:

| Overlay File | Changes |
|-------------|--------|
| `loragw_hal.c` / `.h` | Updated initialization, channel management, Channel E support |
| `loragw_sx1261.c` / `.h` | Extended SX1261 for full RX/TX (was scan/LBT only) |
| `loragw_sx1302.c` / `.h` | Updated concentrator interface |
| `loragw_spi.c` / `.h` | SPI optimized: 16 MHz clock, 16 KB burst chunks |
| `loragw_aux.c` | Added BW_62K5HZ bandwidth support |
| `sx1261_defs.h` | Updated register definitions |
| `lora_pkt_fwd.c` | Channel E packet I/O, spectral scan thread |
| `capture_thread.c` / `.h` | CAPTURE_RAM streaming (disabled for SPI contention avoidance) |
| `Makefile` (libloragw) | Build adjustments |
| `Makefile` (pkt_fwd) | Compile/link capture_thread.o |

## pymc_core Overlay — Differences from Upstream dev

The overlay adds hardware support files. Compared to the upstream `dev` branch:

| File | Status | Description |
|------|--------|-------------|
| `wm1303_backend.py` | **New** (~2970 lines) | Complete WM1303 concentrator backend |
| `virtual_radio.py` | **New** (~198 lines) | VirtualLoRaRadio per-channel abstraction |
| `tx_queue.py` | **New** (~668 lines) | Per-channel TX queue with LBT/CAD |
| `sx1261_driver.py` | **New** (~956 lines) | SX1261 companion radio driver |
| `sx1302_hal.py` | **New** (~37 lines) | HAL wrapper |

These files are added alongside existing hardware drivers (SX1262, KISS, WsRadio).

## pymc_repeater Overlay — Differences from Upstream dev

The overlay modifies existing files and adds new ones:

### Modified Files

| File | Lines Changed | What |
|------|--------------|------|
| `main.py` | +279 lines | Bridge handler, bridge init, SSOT rules loading |
| `bridge_engine.py` | +865 lines | Complete bridge engine (new file replacing minimal upstream) |
| `config.py` | +19 lines | `radio_type: wm1303` in radio factory |
| `sqlite_handler.py` | +144 lines | `dedup_events` table, query/aggregation |
| `http_server.py` | +32 lines | Mount WM1303 API, serve wm1303.html |
| `api_endpoints.py` | +13 lines | WM1303 as hardware selection option |

### New Files

| File | Lines | What |
|------|-------|------|
| `channel_e_bridge.py` | ~200 | Channel E integration |
| `wm1303_api.py` | ~2974 | WM1303 REST API |
| `spectrum_collector.py` | ~275 | Spectral scan collection |
| `cad_calibration_engine.py` | ~150 | CAD calibration |
| `wm1303.html` | ~4000+ | WM1303 Manager UI |
| `config_manager.py` | ~200 | Config management |
| `identity_manager.py` | ~100 | Device identity |
| `packet_router.py` | ~150 | Packet routing |
| `storage_collector.py` | ~100 | Data collection |

## This Repository Structure

```
pyMC_WM1303/
├── overlay/                 # Source overlays (see above)
│   ├── hal/
│   ├── pymc_core/
│   └── pymc_repeater/
├── config/                  # Configuration templates
│   ├── wm1303_ui.json       # Default SSOT config
│   ├── config.yaml.template # Repeater config template
│   ├── global_conf.json     # HAL config template
│   ├── reset_lgw.sh         # GPIO reset script
│   ├── power_cycle_lgw.sh   # Power cycle script
│   └── pymc-repeater.service # systemd unit file
├── docs/                    # Documentation
│   ├── architecture.md
│   ├── radio.md
│   ├── hardware.md
│   ├── software.md
│   ├── configuration.md
│   ├── api.md
│   ├── ui.md
│   ├── lbt_cad.md
│   ├── tx_queue.md
│   ├── installation.md
│   ├── repositories.md
│   ├── channel_e_sx1261.md
│   ├── diagram-style-guide.md
│   └── images/              # Architecture diagrams
├── screenshots/             # UI screenshots
├── scripts/                 # Utility scripts
├── install.sh               # Fresh installation script
├── upgrade.sh               # Upgrade script
├── upgrade_bootstrap.sh     # One-liner upgrade bootstrap
├── bootstrap.sh             # Bootstrap helper
├── README.md                # Project overview
├── TODO.md                  # Task tracking
├── VERSION                  # Current version
├── RELEASE_NOTES.md         # v2.0.0 release notes
├── RELEASE_NOTES_v2.0.1.md  # v2.0.1 release notes
├── RELEASE_NOTES_v2.0.5.md  # v2.0.5 release notes
├── LICENSE                  # License file
└── .gitignore
```

## Version Management

| File | Location | Purpose |
|------|----------|---------|
| `VERSION` | Repository root | Source version |
| `/etc/pymc_repeater/version` | Installed system | Deployed version |

Version format: `MAJOR.MINOR.PATCH` (semantic versioning).

## Related Documents

- [`architecture.md`](./architecture.md) — System architecture
- [`installation.md`](./installation.md) — Installation process
- [`software.md`](./software.md) — Software components
