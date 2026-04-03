# TODO — pyMC WM1303 Bridge/Repeater

> List of open and completed tasks with description of content and motivation.
> Last updated: 2026-04-03

---

## 🔬 Open / Pending

---

### 📡 Radio & Hardware

| # | Item | What | Why | Priority |
|---|------|------|-----|----------|
| 1 | **True hardware CAD** | Implement SX1261 LoRa CAD mode instead of current software histogram analysis | Current CAD is an approximation; true CAD detects LoRa preambles more accurately in ~1ms | Medium |
| 2 | **Duty cycle enforcement** | Per-channel duty cycle tracking with automatic TX blocking when threshold exceeded | EU regulations require <1% (g1) or <10% (g3) — currently only monitored, not enforced | High |
| 3 | **Dynamic TX power** | Adjust TX power based on link quality (RSSI/SNR of received ACK) | Saves airtime, reduces interference for nearby nodes, extends PA lifespan | Low |
| 4 | **RX sensitivity fine-tuning** | IF chain bandwidth/SF optimization specific to each channel | Current IF chain config is generic; channel-specific tuning can yield 1–3 dB improvement | Low |
| 5 | **AD5338R DAC investigation** | Investigate whether the AD5338R DAC is physically present on the WM1303 HAT and what possibilities exist for full-duplex or gain control | WM1302 wiki references `AD5338R_RESET_PIN=13` — CN490 full-duplex reference design. Not in use; GPIO 13 not driven. Potentially relevant for noise floor/TX power optimization | Low |

---

### ⚙️ Backend & Software

| # | Item | What | Why | Priority |
|---|------|------|-----|----------|
| 7 | **Persistent statistics** | Store TX/RX/LBT/dedup stats in SQLite on shutdown and reload on start | All counters reset to 0 on service restart — historical data is lost | Medium |
| 8 | **TX queue priority** | Give ACK packets priority over flood/advertisement messages in the TX queue | ACKs are time-critical; when the queue is full, dropping a flood is preferable to dropping an ACK | Medium |
| 9 | **Adaptive batch window** | Dynamically adjust batch window based on current traffic load | During low traffic, waiting 2s is unnecessarily long; during high traffic it may be too short | Low |
| 10 | **Memory leak check** | Long-running profiling (24–48 hours) of memory usage with tracemalloc or memray | Service runs continuously; small leaks accumulate over days/weeks and can destabilize the Pi | Medium |
| 11 | **Config backup on change** | Automatic timestamped backup of wm1303_ui.json before each save from UI | One wrong setting can break the config; auto-backup enables easy rollback | High |
| 12 | **Escalating recovery system** | Health check cascade: restart pkt_fwd → restart service → reboot Pi | Current watchdog only restarts pkt_fwd; sometimes a full service or Pi reboot is needed for recovery | Medium |
| 13 | **OTA updates** | Update button in UI that executes git pull + rebuild + restart | Currently requires SSH to the Pi for updates; OTA enables remote management without direct access | Low |
| 14 | **Log management** | Log rotation + max size setting for systemd journal and SQLite database | After weeks of continuous operation, logs and DB grow unbounded — SD card can fill up | Medium |
| 15 | **Metrics retention auto-cleanup** | Automatically purge metrics data older than 8 days from the SQLite database | Prevents unbounded database growth on limited SD card storage; 8-day window provides sufficient history for trend analysis while keeping disk usage manageable | High |
| 16 | **Channel friendly names are aliases** | Ensure channel friendly names (e.g., "SF7", "SF8") are treated strictly as display aliases and never used as reference IDs internally | Using aliases as identifiers causes breakage when users rename channels; internal logic must always reference channels by their stable index or RF/IF chain assignment | High |
| 17 | **Frequency decimal separator fix** | Change decimal comma to period for frequency values in RF-Chain and IF-Chain configuration inputs | Some locales render frequency values with a comma separator (e.g., "868,100000") which causes parsing errors; standardize to period notation ("868.100000") for consistency and correctness | Medium |

---

### 🖥️ UI & User Experience

| # | Item | What | Why | Priority |
|---|------|------|-----|----------|
| 18 | **Node tracker** | List/map of all observed MeshCore nodes with RSSI/SNR history per node | Currently only live packets are visible; no overview of known nodes and their signal strength over time | Medium |
| 19 | **Alert system** | Notifications (email/webhook/Telegram) on TX failure, RX dropout, high duty cycle | Problems are only noticed when the UI is opened; alerts provide proactive warning | Medium |
| 20 | **Config export/import** | Download/upload of complete configuration as JSON file | Makes it easy to copy config to a second repeater or restore after a crash | Medium |
| 21 | **Mobile responsive UI** | CSS media queries and layout adjustments for phone/tablet display | Current layout is desktop-optimized; fieldwork and monitoring require mobile access | Low |
| 22 | **System info dashboard** | Show CPU temp, memory, SD card usage, uptime, SPI errors in the UI | Currently requires SSH to check Pi health; system health insight belongs in the UI | Medium |
| 23 | **Dark mode** | Dark theme toggle in the UI header | More comfortable for nighttime use, less eye strain, and lower power consumption on OLED screens | Low |

---

### 📖 Documentation

| # | Item | What | Why | Priority |
|---|------|------|-----|----------|
| 24 | **Comprehensive documentation** | Create a full `docs/` directory with detailed documentation covering all system components (see Documentation section below) | The project has grown significantly; comprehensive docs are essential for maintainability, onboarding, and community contribution | High |
| 25 | **API documentation** | Generate OpenAPI/Swagger spec from all REST endpoints with parameters and response formats | Currently requires reading source code to know endpoints; API docs make integration and debugging easier | Medium |
| 26 | **User manual** | Non-technical guide for daily management of the repeater | Someone else should be able to operate the repeater without all the technical background knowledge | Low |
| 27 | **Network architecture diagram** | Visual overview of the mesh topology with repeater position, channels, and node connections | Helps with planning node placement, channel assignment, and troubleshooting connectivity | Low |

---

### 🔒 Reliability

| # | Item | What | Why | Priority |
|---|------|------|-----|----------|
| 28 | **Hardware watchdog** | Activate Pi hardware watchdog timer (`/dev/watchdog`) with systemd WatchdogSec | If the entire OS freezes, the Pi reboots automatically — currently not the case, requires manual power cycle | High |
| 29 | **Config corruption protection** | Atomic config writes (write to tmp → fsync → rename) + checksum validation on load | Power loss during config save can corrupt the file; atomic writes prevent this | Medium |
| 30 | **Automatic success-version backup** | After X days of stable operation, automatically create a full backup to /home/pi/backups/ | Currently manual; automation prevents forgetting to backup after important changes | Low |

---

### 🧪 Testing & Deployment

| # | Item | What | Why | Priority |
|---|------|------|-----|----------|
| 31 | **Test fresh installation on pi02** | Run install.sh on a clean Raspberry Pi OS Lite installation on hvm-mc-pi02 (192.168.101.235) to validate the complete installation process | The install script has been built against the reference pi01 system; a clean install test on pi02 is essential to verify all phases work on a fresh system without leftover artifacts | High |
| 32 | **Git commit and push all project files** | Commit and push the complete pyMC_WM1303 repository (install.sh, upgrade.sh, overlay/, config/, docs/, README.md, TODO.md) to GitHub | All project files are currently local; pushing to https://github.com/HansvanMeer/pyMC_WM1303 enables version control, collaboration, and deployment from the repository | High |

---

### 📝 Documentation Topics Roadmap

The comprehensive documentation (item #24) should cover the following topics, organized into logical sections:

| Category | Topics |
|----------|--------|
| **Architecture** | WM1303 backend (role & integration with pyMC_core dev, pyMC_Repeater dev), RF-/IF-chains, SPI bus (2 MHz — bandwidth justification), programming languages used |
| **Hardware** | WM1303-specific modifications, HAL driver (what was changed and why), pkt_fwd (what was changed and why), FEM/LNA/AGC/PA issues and LUT tables, SX1261 as spectrum analyzer (CAD/spectral scan and where they are used) |
| **Radio** | RX CRC errors, max 4 channels (fewer = better/more stable), MeshCore sync word, LoRaWAN compatibility, VirtualLoRaRadio, TX/RX echo, noise floor/RSSI/SNR per channel, channel configuration (settings per channel), LBT/CAD operation in TX queue |
| **Software** | Config files, TX hold (default 4s), TX queues scheduler, bridge rules (SSOT) and packet type selection, pyMC_core modifications, pyMC_Repeater modifications, database logging (for metrics in dashboard/graphs) |
| **Integration** | How integrated into MeshCore, WM1303 API, WM1303 Manager (UI), repos (pyMC_core dev, pyMC_Repeater dev, HAL v2.10), JWT token coupling |
| **Legal** | No responsibility disclaimer for potential hardware damage when using this code |

---
## ✅ Completed
| # | Title | What & Why |
|---|-------|------------|
| 1 | **Project setup** | Cloned repos (pyMC_Repeater, pyMC_core), configured Pi SSH access. Required as the foundation for all further development. |
| 2 | **Systemd service** | Hardened `pymc-repeater.service` unit file + `install_sensecap_m1.sh` script. Ensures automatic startup, crash recovery, and secured runtime (NoNewPrivileges, PrivateTmp, etc.). |
| 3 | **HAL debug tools** | SPI probing scripts, GPIO reset test, HAL shared library validation. Needed to determine which SPI device (`spidev0.0` vs `0.1`) the SX1302 uses and whether the HAL initializes correctly. |
| 4 | **WM1303 integration** | First version of `wm1303_backend.py` (HAL control), `wm1303_api.py` (REST endpoints), `wm1303.html` (web UI). Replaces the standalone packet forwarder with an integrated Python-driven system. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **RF chain split** | Channel A on RF0, Channel B on RF1. The SX1302 has 2 RF chains that can each independently receive a frequency. Splitting them allows 2 LoRa channels to be active simultaneously. |
| 2 | **SX1261 initialization** | Companion chip on `spidev0.1` configured for spectral scan. The SX1261 can scan the RF spectrum while the SX1302 handles RX/TX, needed for noise floor measurements and LBT. |
| 3 | **Config sync** | `global_conf.json` (HAL) and `bridge_conf.json` (runtime) automatically synchronized on changes from the UI. Previously configs had to be updated manually. |
| 4 | **API endpoint fix** | 10 missing endpoints restored, 3 recurring errors resolved. API calls from the UI failed on endpoints that existed in the HTML but not in the backend. |
| 5 | **Phase 2 integration** | WebSocket handler (ws4py) for realtime updates, TextHelper fix for MeshCore messages, bridge stats in API. Needed for live data in the UI without polling. |
| 6 | **Packet counting** | Red dot indicator fix (always showed red), TX advert counting corrected, bridge status attribute fix. Statistics in the UI did not match actual RX/TX counts. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **Single Source of Truth** | `wm1303_ui.json` as central configuration. Previously channel settings were scattered across multiple files (global_conf, bridge_conf, Python code). Now there is one source. |
| 2 | **Bridge SSOT coupling** | Bridge rules loaded from `wm1303_ui.json` instead of hardcoded. After SSOT, the bridge engine also needed to read from the central config file. |
| 3 | **Bridge forwarding bugs** | Packets were not correctly forwarded between channels. Cause: incorrect channel matching in bridge rules evaluation. |
| 4 | **Repeater handler** | MeshCore hop count +1, path bytes updated on forwarding. Without this, nodes would not know the packet came through a repeater. |
| 5 | **Routed repeater mode** | MeshCore protocol-specific processing: advertisement packets, route paths, flood messages. Required for correct mesh network integration. |
| 6 | **RX/TX routing** | Packets arrived on the wrong channel or were sent to the wrong channel. Cause: frequency-to-channel mapping was incorrect. |
| 7 | **Channels tab redesign** | Complete UI redesign with per-channel status cards, live statistics, configuration options. Old tab was a simple table without interaction. |
| 8 | **Spectrum tab** | Spectral scan graph + waterfall view added. Visualizes the RF spectrum to identify interference and noise floor. |
| 9 | **Chart.js bugs** | Spectrum chart did not render correctly: wrong axes, missing data points, memory leak on updates. |
| 10 | **CRC validation** | Packet CRC check was disabled, allowing corrupt packets to be accepted. Now only CRC-OK packets are processed. |
| 11 | **Channel B RX** | Channel B received no packets. Cause: RF chain 1 was not correctly configured for the proper frequency and SF. |
| 12 | **Channel B UDP pipe** | UDP forwarding for Channel B packets was not working. The packet forwarder only forwarded RF0 packets. |
| 13 | **RF chain assignment** | IF chains were assigned to the wrong RF chain. IF 0–3 should be on RF0, IF 4–7 on RF1 — was reversed. |
| 14 | **Channel naming** | RX dispatch used wrong channel names. "channel_a" and "channel_b" did not match UI names ("SF8", "SF7"). |
| 15 | **TX power UI** | Dropdown in UI to select TX power per channel (2–27 dBm). Previously TX power was hardcoded at 14 dBm. |
| 16 | **AGC recalibration** | First attempt to restore Automatic Gain Control after TX. After each TX, the AGC goes out of calibration, causing RX sensitivity to drop. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **PULL_RESP format** | TX packets were rejected by the packet forwarder. PULL_RESP JSON structure was incorrect (wrong field names, missing `imme` flag). |
| 2 | **Global TX Scheduler** | Round-robin TX replaces per-channel queues. 50ms inter-packet gap prevents rapid successive TXs from destabilizing the AGC. |
| 3 | **TX batch window** | 2-second wait time during bridge forwarding so all target channels are queued simultaneously. Prevents a packet on channel A from being sent before channel B receives it. |
| 4 | **Echo prevention** | Self-echo hash prevents bridge loops. Without this, a forwarded packet would be received again and forwarded endlessly. |
| 5 | **IF demodulator config** | IF chains had wrong SF/BW settings. Multi-SF demodulators must match channel configuration. |
| 6 | **Multiple IF chains** | Each RF chain can use 4 IF demodulators. Configuration expanded from 1 to 4 per RF chain for better reception. |
| 7 | **RF chain configuration** | RF0 + RF1 settings: frequency, type (SX1250), RSSI offset, TX enable. Both chains must be correctly configured. |
| 8 | **SF name mapping** | Spreading Factor names ("SF7", "SF8") did not match between UI, config, and backend code. Unified to one mapping. |
| 9 | **Realtime statistics** | Live stats (RX count, RSSI, SNR) were not updating in UI. WebSocket push was not connected to stats update events. |
| 10 | **LBT first version** | Listen Before Talk: RSSI measurement before TX. Legally required in EU 868 MHz band (but implementation not yet complete). |
| 11 | **LBT per-channel toggle** | Checkbox in UI to enable/disable LBT per channel. Not every channel needs LBT (depends on frequency/band). |
| 12 | **TX enable/disable** | Channel TX could not be disabled via UI. The backend ignored the `tx_enabled` flag from the config. |
| 13 | **AGC disable** | Option to turn off AGC recalibration. AGC caused instability after TX; disabling it gave more stable RX. |
| 14 | **Double AGC reload** | AGC was executed twice after TX (once by HAL, once by our code). Caused double RX interruptions. |
| 15 | **Concentrator reset** | GPIO-based power cycle of the SX1302. Needed when the concentrator enters an unrecoverable state. |
| 16 | **Advertisement interval** | MeshCore advertisement packets were sent too often/infrequently. Timer was not correctly configured after bridge integration. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **Software LBT** | Per-channel RSSI check via SX1261 spectral scan data. HAL-level LBT conflicted with multi-channel setup; software LBT provides more control. |
| 2 | **LBT decisions chart** | Visualization of LBT pass/block over time per channel. Helps with tuning LBT thresholds. |
| 3 | **LBT default off** | LBT was enabled by default for all channels, blocking TX on channels where it wasn't needed. Now default off, per channel configurable. |
| 4 | **HAL LBT disabled** | HAL-level LBT caused "Cannot start LBT - wrong channel" errors for channels without LBT config. `lbt.enable=false` in global_conf.json; software LBT takes over. |
| 5 | **SX1261 fully active** | SX1261 chip enabled for spectral scan in global_conf.json. Was only activated in bridge_conf but not in the HAL config that actually controls the SX1261. |
| 6 | **TX always via RF0** | TX sometimes went via RF1 (which has no TX frontend). Forced to RF chain 0 which drives the PA (Power Amplifier). |
| 7 | **TX timestamps** | TX timestamps did not match the actual transmit moment. Cause: timestamp was set at queue insert instead of actual TX. |
| 8 | **TX scheduling refinement** | Inter-packet gap and TX timing further optimized. Reduces AGC instability through better spacing of TX moments. |
| 9 | **Signal quality dashboard** | RSSI/SNR visualization per channel, channel stats history in SQLite, background snapshot thread. Enables trending and analysis of signal strength over time. |
| 10 | **Unified statistics** | Live channel stats and TX queue stats merged into one API response. Reduces number of API calls and prevents inconsistency. |
| 11 | **FIFO ordering** | TX queue sometimes processed packets in wrong order. Collections.deque replaces list-based queue for correct FIFO. |
| 12 | **Spectrum tab v2** | Improved spectral scan visualization: better color scale, zoom function, frequency markers per channel. |
| 13 | **LNA register correction** | FEM (Front-End Module) LNA register had wrong value. Resulted in suboptimal RX sensitivity (~5 dB loss). |
| 14 | **LNA 0x0F + UI control** | LNA gain register set to 0x0F (maximum gain) + UI toggle to switch LNA modes. Experiments with optimal RX sensitivity. |
| 15 | **LNA back to 0x02** | 0x0F caused too much noise; 0x02 provides better balance between gain and noise figure. |
| 16 | **Deduplication bugs** | Packets were sometimes incorrectly marked as duplicate (too broad hash match) or not detected (too narrow window). |
| 17 | **AGC defaults + HAL optimization** | AGC reverted to HAL defaults after experimental implementations. HAL configuration optimized for stability. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **AGC auto-recovery** | Automatic recovery when AGC enters a bad state. Detects AGC failure via RSSI anomalies and triggers reinitialization. |
| 2 | **AGC robustness test** | Stress test with high TX frequency to validate AGC recovery. Confirms AGC recovers within 50ms after TX. |
| 3 | **AGC speed** | AGC recalibration reduced from ~200ms to ~50ms. Less RX loss after each TX through faster gain settling. |
| 4 | **Chart.js bugs** | Multiple charts (signal quality, spectrum, LBT) had rendering issues: wrong data binding, memory leaks, tooltip errors. |
| 5 | **LNA final config** | Register 0x02 as final choice after extensive A/B tests (0x02 vs 0x03 vs 0x0F). Best balance: -3 dB noise figure, +12 dB gain. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **AGC health monitor** | Periodic AGC check (every 10s): measures RSSI baseline, compares with expected value, triggers recalibration if deviation >5 dB. |
| 2 | **FEM register forced** | Front-End Module LNA/PA registers are now rewritten on every TX/RX switch. Previously they sometimes became corrupt after frequent switching. |
| 3 | **FEM non-blocking** | FEM initialization blocked RX for ~100ms. Now executed asynchronously so RX resumes immediately after TX. |
| 4 | **LNA LUT register** | Look-Up Table register set to 0x03 for experiments with per-signal-strength gain adjustment. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **AGC off + TX batching** | AGC fully disabled (caused more problems than it solved), combined with improved TX batch scheduling. Most stable configuration to date. |
| 2 | **MCU firmware reload** | SX1302 internal microcontroller firmware reloaded after AGC corruption. Deeper reset than just register writing. |
| 3 | **LNA LUT revert** | Back to 0x03 after analysis: 0x02 gave ~1 dB better gain but 2 dB more noise. 0x03 is the better compromise. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **P1-P5 HAL optimization** | 5 priorities tackled simultaneously: (P1) AGC stability, (P2) FEM register retention, (P3) IF chain fine-tuning, (P4) spectral scan timing, (P5) TX/RX switching. Results in the most stable HAL configuration. |
| 2 | **Final IF chain mapping** | IF chains definitively locked: IF0-3 on RF0 (Channel A), IF4-7 on RF1 (Channel B). No more dynamic reassignment; fixed mapping prevents configuration drift. |
| 3 | **Dedup chart** | Deduplication event visualization in UI with SQLite storage. Shows when and how often packets are detected as duplicates. Helps with tuning the dedup window. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **RX Watchdog** | 3 automatic detection modes: (1) PUSH_DATA statistics (2× rxnb=0 with active TX), (2) RSSI spike detection (5+ strong signals without successful RX), (3) RX timeout (180s no packet). On detection: automatic packet forwarder restart. Previously required manual service restart on RX failure. |
| 2 | **NF + CAD + LBT integration** | Noise Floor monitoring, Channel Activity Detection, and Listen Before Talk as one integrated system. Per-channel toggles, adaptive thresholds, rolling buffers. Replaces separate implementations that conflicted. |
| 3 | **CAD chart** | Channel Activity Detection visualization: shows when LoRa activity is detected per channel. Helps determine if LBT/CAD are correctly configured. |
| 4 | **Chart.js race condition** | Charts crashed on tab switching: Chart.js + date adapter were dynamically loaded but not ready when the chart initialized. Fix: wait for library load before chart init. |
| 5 | **JSON serialization** | API returned 500 errors for datetime and numpy objects. Python's json.dumps cannot serialize these types. Fix: custom JSON encoder converting datetime→ISO string and numpy→float. |
| 6 | **AGC debounce** | HAL C-code patch in loragw_hal.c: AGC recalibration is debounced (minimum 100ms between recalibrations). Prevents oscillation during rapid successive RX/TX events. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **Dev branch migration** | pyMC_Repeater + pyMC_core from main → dev branch with 7 patches applied. All local changes committed to dev so they are not lost during updates. |
| 2 | **MeshCore hop count** | Bridge repeater handler adjusted hop count incorrectly: +2 instead of +1 per hop. Nodes therefore calculated wrong route lengths and chose suboptimal paths. |
| 3 | **Config save via UI** | Save button in UI that saves bridge configuration to wm1303_ui.json + triggers service restart. Previously required SSH to the Pi and manual restart. |
| 4 | **IF chain index mapping** | After config change in UI, IF chain indices were calculated incorrectly: index 0-3 became 1-4. Cause: off-by-one in the config generator. Resulted in wrong SF per IF chain. |
| 5 | **MQTT fully removed** | MQTT tab, JavaScript handlers, API endpoints (`/api/wm1303/mqtt/*`), config entries from config.yaml. MQTT was not in use and caused import errors and UI confusion. |
| 6 | **Spectral scan activation** | SX1261 spectral scan enabled in global_conf.json with correct parameters (freq_start=863MHz, freq_stop=870MHz, nb_chan=36, pace_s=1). Was configured in bridge_conf but not in the HAL config that actually controls the SX1261. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **Channels tab UI updates** | 6 changes: (1) decimal comma→period conversion for input fields, (2) IF Chain Configuration block repositioned, (3) Radio Summary block with total RX/TX, (4) TX airtime and duty cycle, (5) adaptive refresh, (6) noise floor guard against -120 fallback. |
| 2 | **Dedup chart data** | Dedup chart showed no data: `set_sqlite_handler()` was missing in main.py + `dedup_events` table did not exist. After fix: realtime dedup events visible in chart. |
| 3 | **Status tab totals** | RX/TX counters now show total across all active channels (was per-channel). Version header shows only "v0.9.315" instead of "WM1303 Manager v0.9.315". |
| 4 | **TX Queue overflow fix** | Queue size from 50→15, TTL from 30s→5s, overflow policy from "reject newest"→"drop oldest". Cause of TX problem: 3400+ packets per channel failed due to full queues. After fix: 0 failed, 0 pending. Nodes receive ACKs again. |
| 5 | **LBT RSSI as Noise Floor (Option B)** | Real noise floor measurements instead of -120 dBm fallback. NoiseFloorMonitor (30s interval) sets 4s TX hold, SX1261 spectral scan harvest, per-channel freq matching, RSSI→rolling buffer (20 samples). Freq-to-UI-name cache resolves naming mismatch. Result: ch-a=-93.4, ch-b=-100.5, ch-d=-118.2 dBm. |
| 6 | **Auto-update LBT/CAD** | Per-channel LBT/CAD settings from IF Chain Configuration are automatically reloaded with 5s cache TTL. No service restart needed after changes in UI. |
| 7 | **LBT RSSI deploy** | Updated wm1303_backend.py + wm1303.html with real noise floor values, color coding (green <-110, yellow <-90, red ≥-90), and "--" instead of -120 when no data available. |
| 8 | **TX_Queue_Flow.md** | 452 lines of documentation on the complete TX processing flow: RX receipt → Bridge Engine → TX Queue (9 steps) → Radio TX. Including timing, SPI bus analysis, background processes, API statistics, configuration, and troubleshooting. |
| # | Title | What & Why |
|---|-------|------------|
| 1 | **pyMC_WM1303 repository created** | Clean GitHub repository with comprehensive installation script (`install.sh`, 12 phases) for deploying WM1303 LoRa concentrator with MeshCore on SenseCAP M1 / Raspberry Pi. Separates deployment artifacts from the development project. |
| 2 | **Overlay directory structure** | Created complete overlay directory with all modified HAL, pyMC_core, and pyMC_Repeater files extracted from the reference pi01 system. Organized into `overlay/hal/`, `overlay/pymc_core/`, and `overlay/pymc_repeater/` for clean separation. |
| 3 | **Configuration templates** | Created `config.yaml.template`, `wm1303_ui.json`, `global_conf.json`, and `pymc-repeater.service` in the `config/` directory. Templates use placeholder values for site-specific settings while preserving all proven production parameters. |
| 4 | **Upgrade script** | Created `upgrade.sh` with 8 phases: backup current state, stop services, update git repositories, re-apply overlay modifications, rebuild HAL and pkt_fwd, reinstall Python packages, update configuration files, restart services. Enables safe updates when upstream repos change. |
| 5 | **Overlay verification** | Verified all overlay files against pi01 reference system. 100% match confirmed across all HAL source modifications, pyMC_core hardware modules, and pyMC_Repeater integration files. |
| 6 | **Missing __init__.py fix** | Found and fixed missing `__init__.py` in pymc_core overlay (`overlay/pymc_core/src/pymc_core/hardware/`). Added proper imports for WM1303Backend and VirtualLoRaRadio classes to ensure Python package discovery works correctly during installation. |
| 7 | **GPIO pin configuration via Adv. Config tab** | Added Group 5 (GPIO Pin Configuratie) to the WM1303 Manager's Adv. Config tab with configurable BCM pin numbers for SX1302 Reset, SX1302 Power Enable, SX1261 Reset, AD5338R Reset, and GPIO Base Offset. Includes live sysfs number preview, hardware warning dialog, and auto-regeneration of `reset_lgw.sh` and `power_cycle_lgw.sh` when pins are changed. API endpoint extended with GET/POST support for `gpio_pins` group. |
