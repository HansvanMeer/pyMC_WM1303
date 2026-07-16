"""SX1261 LoRa radio driver for WM1303 Pi HAT.

Drives the SX1261 radio on /dev/spidev0.1 via raw SPI commands.

RF0=RX / RF1=TX Split Architecture:
  - TX is now handled by RF1 (SX1250_1) via PULL_RESP to lora_pkt_fwd
  - SX1261 is repurposed for LBT, CAD, and Spectrum Analysis ONLY
  - TX methods are kept for backward compatibility but deprecated

Available LBT/CAD Methods:
  - listen_before_talk(freq_hz, threshold_dbm, timeout_ms) -> bool
  - channel_activity_detection(freq_hz) -> bool
  - get_rssi(freq_hz) -> float
  - get_rssi_scan(freq_start, freq_end, step_hz) -> list

Reference: Semtech SX1261/SX1262 Datasheet DS.SX1261-2.W.APP (Rev 2.1)
Command set: Section 13 - SPI Interface
"""
from __future__ import annotations

import logging
import struct
import os
import time
from typing import Optional
from openhop_core.paths import resolve_config_path  # WM1303 v2.7: central config-path helper

try:
    import spidev
except ImportError:
    spidev = None

# Region-aware image calibration (Issue #4 multi-region support)
try:
    from .region_config import get_sx1261_calib as _region_get_sx1261_calib
    _REGION_CALIB_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REGION_CALIB_AVAILABLE = False


def _read_region_from_ui() -> tuple[int, int]:
    """Read regulatory region from wm1303_ui.json and return SX1261 calibration bytes.

    Returns (cal_low, cal_high) byte values for CMD_CALIBRATE_IMAGE.
    Falls back to EU868 [0xD7, 0xDB] if region_config or UI JSON unavailable.
    """
    if not _REGION_CALIB_AVAILABLE:
        return 0xD7, 0xDB  # EU868 fallback
    try:
        import json as _json
        from pathlib import Path as _Path
        # UI JSON path: /etc/openhop_repeater/wm1303_ui.json (canonical) or /etc/pymc_repeater/wm1303_ui.json (legacy fallback)
        # or relative override during development
        _candidates = [
            _Path(os.environ.get("WM1303_UI_JSON", "")),
            resolve_config_path('wm1303_ui.json'),
            _Path("/opt/pymc_repeater/wm1303_ui.json"),
        ]
        ui_data = None
        center_hz = None
        for p in _candidates:
            if p and str(p) and p.exists():
                ui_data = _json.loads(p.read_text())
                break
        if ui_data is None:
            return 0xD7, 0xDB  # EU868 fallback
        # Get region (string or {'code': ..., ...})
        _region = ui_data.get("region", "EU868")
        if isinstance(_region, dict):
            _region_code = str(_region.get("code", "EU868")).upper()
        else:
            _region_code = str(_region or "EU868").upper()
        # Center freq fallback for CUSTOM region
        _rf_mhz = ui_data.get("rf_center_freq_mhz", 0)
        if _rf_mhz:
            center_hz = int(float(_rf_mhz) * 1_000_000)
        return _region_get_sx1261_calib(_region_code, fallback_center_hz=center_hz)
    except Exception:
        return 0xD7, 0xDB  # Safe fallback

try:
    import gpiod
    _HAS_GPIOD = True
except ImportError:
    _HAS_GPIOD = False

logger = logging.getLogger("SX1261Driver")

# ─── SX126x SPI Commands (Datasheet Section 13.4) ─────────────────────
CMD_SET_SLEEP             = 0x84
CMD_SET_STANDBY           = 0x80
CMD_SET_FS                = 0xC1
CMD_SET_TX                = 0x83
CMD_SET_RX                = 0x82
CMD_STOP_TIMER_ON_PREAMBLE = 0x9F
CMD_SET_CAD               = 0xC5
CMD_SET_TX_CONTINUOUS     = 0xD1
CMD_SET_TX_INFINITE_PREAMBLE = 0xD2
CMD_SET_REGULATOR_MODE    = 0x96
CMD_CALIBRATE             = 0x89
CMD_CALIBRATE_IMAGE       = 0x98
CMD_SET_PA_CONFIG         = 0x95
CMD_SET_RX_TX_FALLBACK_MODE = 0x93

# DIO & IRQ
CMD_SET_DIO_IRQ_PARAMS    = 0x08
CMD_GET_IRQ_STATUS        = 0x12
CMD_CLR_IRQ_STATUS        = 0x02
CMD_SET_DIO2_AS_RF_SWITCH = 0x9D
CMD_SET_DIO3_AS_TCXO      = 0x97

# Packet & Modulation
CMD_SET_RF_FREQUENCY      = 0x86
CMD_SET_PKT_TYPE          = 0x8A
CMD_GET_PKT_TYPE          = 0x11
CMD_SET_TX_PARAMS         = 0x8E
CMD_SET_MOD_PARAMS        = 0x8B
CMD_SET_PKT_PARAMS        = 0x8C
CMD_SET_CAD_PARAMS        = 0x88
CMD_SET_BUFFER_BASE_ADDR  = 0x8F
CMD_SET_LORA_SYMB_TIMEOUT = 0xA0

# Status
CMD_GET_STATUS            = 0xC0
CMD_GET_RSSI_INST         = 0x15
CMD_GET_RX_BUFFER_STATUS  = 0x13
CMD_GET_PKT_STATUS        = 0x14
CMD_GET_DEVICE_ERRORS     = 0x17
CMD_CLR_DEVICE_ERRORS     = 0x07

# Buffer
CMD_WRITE_BUFFER          = 0x0E
CMD_READ_BUFFER           = 0x1E

# Register
CMD_WRITE_REGISTER        = 0x0D
CMD_READ_REGISTER         = 0x1D

# ─── IRQ Bit Masks ─────────────────────────────────────────────────────
IRQ_TX_DONE               = 0x0001
IRQ_RX_DONE               = 0x0002
IRQ_PREAMBLE_DETECTED     = 0x0004
IRQ_SYNC_WORD_VALID       = 0x0008
IRQ_HEADER_VALID          = 0x0010
IRQ_HEADER_ERR            = 0x0020
IRQ_CRC_ERR               = 0x0040
IRQ_CAD_DONE              = 0x0080
IRQ_CAD_DETECTED          = 0x0100
IRQ_TIMEOUT               = 0x0200
IRQ_ALL                   = 0x03FF

# ─── Constants ─────────────────────────────────────────────────────────
STDBY_RC                  = 0x00
STDBY_XOSC                = 0x01

PKT_TYPE_GFSK             = 0x00
PKT_TYPE_LORA             = 0x01

# PA config for SX1261 (low-power PA): max +15 dBm
PA_DUTY_CYCLE_SX1261      = 0x04
PA_HP_MAX_SX1261          = 0x00  # SX1261 doesn't have HP PA
PA_DEVICE_SEL_SX1261      = 0x01  # 0x01 = SX1261
PA_LUT_SX1261             = 0x01

# Regulator mode
REG_MODE_LDO              = 0x00
REG_MODE_DC_DC            = 0x01

# LoRa Bandwidth encoding (Section 13.4.5.2)
LORA_BW_7K8   = 0x00
LORA_BW_10K4  = 0x08
LORA_BW_15K6  = 0x01
LORA_BW_20K8  = 0x09
LORA_BW_31K25 = 0x02
LORA_BW_41K7  = 0x0A
LORA_BW_62K5  = 0x03
LORA_BW_125K  = 0x04
LORA_BW_250K  = 0x05
LORA_BW_500K  = 0x06

# LoRa Coding Rate
LORA_CR_4_5   = 0x01
LORA_CR_4_6   = 0x02
LORA_CR_4_7   = 0x03
LORA_CR_4_8   = 0x04

# Register addresses
REG_LORA_SYNC_WORD_MSB = 0x0740
REG_LORA_SYNC_WORD_LSB = 0x0741
REG_OCP                = 0x08E7  # Over-current protection
REG_XTA_TRIM           = 0x0911
REG_XTB_TRIM           = 0x0912

# ─── Bandwidth mapping ─────────────────────────────────────────────────
BW_KHZ_TO_REG = {
    7.8:   LORA_BW_7K8,
    10.4:  LORA_BW_10K4,
    15.6:  LORA_BW_15K6,
    20.8:  LORA_BW_20K8,
    31.25: LORA_BW_31K25,
    41.7:  LORA_BW_41K7,
    62.5:  LORA_BW_62K5,
    125:   LORA_BW_125K,
    125.0: LORA_BW_125K,
    250:   LORA_BW_250K,
    250.0: LORA_BW_250K,
    500:   LORA_BW_500K,
    500.0: LORA_BW_500K,
}

# Coding rate mapping
CR_TO_REG = {
    5: LORA_CR_4_5,   # 4/5
    6: LORA_CR_4_6,   # 4/6
    7: LORA_CR_4_7,   # 4/7
    8: LORA_CR_4_8,   # 4/8
}


class SX1261Radio:
    """SX1261 LoRa radio driver via SPI (LBT/CAD primary, TX deprecated).

    Designed for the WM1303 Pi HAT where SX1261 is on /dev/spidev0.1
    with GPIO5 as reset pin.

    In the RF0=RX / RF1=TX architecture:
      - TX goes through RF1 via PULL_RESP (not SX1261)
      - SX1261 handles LBT, CAD, and Spectrum Analysis
      - TX methods are kept for backward compatibility
    """

    def __init__(
        self,
        spi_bus: int = 0,
        spi_cs: int = 1,
        reset_gpio: int = 5,
        gpio_chip: str = "gpiochip0",
        spi_speed_hz: int = 2_000_000,
    ):
        self._spi_bus = spi_bus
        self._spi_cs = spi_cs
        self._reset_gpio = reset_gpio
        self._gpio_chip = gpio_chip
        self._spi_speed = spi_speed_hz
        self._spi: Optional[spidev.SpiDev] = None
        self._gpio_line = None
        self._initialized = False

        # Current configuration (to avoid redundant reconfiguration)
        self._cur_freq_hz: int = 0
        self._cur_bw_khz: float = 0
        self._cur_sf: int = 0
        self._cur_cr: int = 0
        self._cur_sync_word: int = 0
        self._cur_preamble_len: int = 0
        self._cur_tx_power: int = 0

        # Stats
        self.tx_count: int = 0
        self.tx_errors: int = 0
        self.last_tx_time: float = 0

    # ─── Lifecycle ─────────────────────────────────────────────────

    def init(self) -> bool:
        """Initialize the SX1261: reset, configure for LoRa TX."""
        if spidev is None:
            raise RuntimeError("spidev library not installed (pip install spidev)")

        try:
            # Open SPI
            self._spi = spidev.SpiDev()
            self._spi.open(self._spi_bus, self._spi_cs)
            self._spi.max_speed_hz = self._spi_speed
            self._spi.mode = 0  # CPOL=0, CPHA=0
            self._spi.bits_per_word = 8
            self._spi.no_cs = False
            logger.info("SX1261: SPI opened /dev/spidev%d.%d @ %d Hz",
                       self._spi_bus, self._spi_cs, self._spi_speed)

            # Hardware reset
            self._hw_reset()

            # Wait for chip ready (check busy via status)
            self._wait_busy(timeout=1.0)

            # Set to STDBY_RC
            self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            self._wait_busy()

            # Set packet type to LoRa
            self._cmd(CMD_SET_PKT_TYPE, [PKT_TYPE_LORA])
            self._wait_busy()

            # Set regulator mode to DC-DC (more efficient)
            self._cmd(CMD_SET_REGULATOR_MODE, [REG_MODE_DC_DC])
            self._wait_busy()

            # Set DIO2 as RF switch control (common on WM1303)
            self._cmd(CMD_SET_DIO2_AS_RF_SWITCH, [0x01])
            self._wait_busy()

            # Calibrate image for the configured regulatory region (Issue #4)
            # Reads region from wm1303_ui.json; defaults to EU868 [0xD7, 0xDB].
            _cal_low, _cal_high = _read_region_from_ui()
            logger.info(
                "SX1261: image calibration for region (cal_low=0x%02X, cal_high=0x%02X)",
                _cal_low, _cal_high,
            )
            self._cmd(CMD_CALIBRATE_IMAGE, [_cal_low, _cal_high])
            self._wait_busy(timeout=1.0)

            # Full calibration
            self._cmd(CMD_CALIBRATE, [0x7F])  # All blocks
            self._wait_busy(timeout=2.0)

            # Set buffer base addresses
            self._cmd(CMD_SET_BUFFER_BASE_ADDR, [0x00, 0x00])  # TX base, RX base
            self._wait_busy()

            # Configure PA for SX1261 (low-power PA, max +15 dBm)
            self._cmd(CMD_SET_PA_CONFIG, [
                PA_DUTY_CYCLE_SX1261,  # paDutyCycle
                PA_HP_MAX_SX1261,      # hpMax
                PA_DEVICE_SEL_SX1261,  # deviceSel (0x01 = SX1261)
                PA_LUT_SX1261,         # paLut
            ])
            self._wait_busy()

            # Set fallback mode to STDBY_RC after TX
            self._cmd(CMD_SET_RX_TX_FALLBACK_MODE, [0x20])  # STDBY_RC
            self._wait_busy()

            # Clear all IRQs
            self._cmd(CMD_CLR_IRQ_STATUS, [0x03, 0xFF])
            self._wait_busy()

            # Set IRQ: TX_DONE + TIMEOUT on DIO1
            # SetDioIrqParams(irqMask, dio1Mask, dio2Mask, dio3Mask)
            irq_mask = IRQ_TX_DONE | IRQ_TIMEOUT
            self._cmd(CMD_SET_DIO_IRQ_PARAMS, [
                (irq_mask >> 8) & 0xFF, irq_mask & 0xFF,  # irqMask
                (irq_mask >> 8) & 0xFF, irq_mask & 0xFF,  # dio1Mask
                0x00, 0x00,  # dio2Mask
                0x00, 0x00,  # dio3Mask
            ])
            self._wait_busy()

            # Clear device errors
            self._cmd(CMD_CLR_DEVICE_ERRORS, [0x00])
            self._wait_busy()

            self._initialized = True
            logger.info("SX1261: initialized successfully")
            return True

        except Exception as e:
            logger.error("SX1261: init failed: %s", e, exc_info=True)
            self._initialized = False
            return False

    def close(self) -> None:
        """Put radio to sleep and release resources."""
        if self._spi:
            try:
                self._cmd(CMD_SET_SLEEP, [0x00])  # Cold start sleep
            except Exception:
                pass
            try:
                self._spi.close()
            except Exception:
                pass
            self._spi = None
        if self._gpio_line:
            try:
                self._gpio_line.release()
            except Exception:
                pass
            self._gpio_line = None
        self._initialized = False
        logger.info("SX1261: closed")

    # ─── Configuration ─────────────────────────────────────────────

    def configure(
        self,
        freq_hz: int,
        bw_khz: float = 125.0,
        sf: int = 8,
        cr: int = 5,
        sync_word: int = 0x1424,
        preamble_len: int = 17,
        tx_power_dbm: int = 14,
    ) -> None:
        """Configure the radio for TX with given parameters.

        Only reconfigures parameters that have changed since last call.
        """
        if not self._initialized:
            raise RuntimeError("SX1261 not initialized")

        # Go to standby before reconfiguring
        self._cmd(CMD_SET_STANDBY, [STDBY_RC])
        self._wait_busy()

        # Frequency
        if freq_hz != self._cur_freq_hz:
            self._set_frequency(freq_hz)
            self._cur_freq_hz = freq_hz

        # Modulation params (SF, BW, CR)
        if bw_khz != self._cur_bw_khz or sf != self._cur_sf or cr != self._cur_cr:
            self._set_modulation_params(sf, bw_khz, cr)
            self._cur_bw_khz = bw_khz
            self._cur_sf = sf
            self._cur_cr = cr

        # Sync word
        if sync_word != self._cur_sync_word:
            self._set_sync_word(sync_word)
            self._cur_sync_word = sync_word

        # TX power
        if tx_power_dbm != self._cur_tx_power:
            self._set_tx_power(tx_power_dbm)
            self._cur_tx_power = tx_power_dbm

        # Preamble length is set per-packet in _set_packet_params
        self._cur_preamble_len = preamble_len

        logger.debug("SX1261: configured freq=%d bw=%.1f sf=%d cr=%d sw=0x%04X preamble=%d power=%d",
                    freq_hz, bw_khz, sf, cr, sync_word, preamble_len, tx_power_dbm)

    def send(self, payload: bytes, timeout_s: float = 5.0) -> bool:
        """DEPRECATED: Send a packet via SX1261.

        In the RF0=RX / RF1=TX architecture, TX goes through RF1 via PULL_RESP.
        This method is kept for backward compatibility only.

        Send a packet and wait for TX done.

        Args:
            payload: Raw bytes to transmit (max 255 bytes).
            timeout_s: Maximum time to wait for TX completion.

        Returns:
            True if TX completed successfully, False on timeout/error.
        """
        if not self._initialized:
            raise RuntimeError("SX1261 not initialized")

        if len(payload) > 255:
            raise ValueError(f"Payload too large: {len(payload)} bytes (max 255)")

        try:
            # Ensure standby
            self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            self._wait_busy()

            # Set packet params for this payload
            self._set_packet_params(
                preamble_len=self._cur_preamble_len,
                payload_len=len(payload),
                crc_on=True,
                invert_iq=False,
            )
            self._wait_busy()

            # Write payload to TX buffer at offset 0
            self._write_buffer(0x00, payload)
            self._wait_busy()

            # Clear IRQs
            self._cmd(CMD_CLR_IRQ_STATUS, [0x03, 0xFF])
            self._wait_busy()

            # Start TX (timeout in ms * 15.625us steps)
            # timeout_val = timeout_ms * 1000 / 15.625 = timeout_ms * 64
            timeout_ms = int(timeout_s * 1000)
            timeout_val = min(timeout_ms * 64, 0xFFFFFF)  # 24-bit max
            self._cmd(CMD_SET_TX, [
                (timeout_val >> 16) & 0xFF,
                (timeout_val >> 8) & 0xFF,
                timeout_val & 0xFF,
            ])

            # Poll for TX done
            start = time.monotonic()
            while time.monotonic() - start < timeout_s:
                irq = self._get_irq_status()
                if irq & IRQ_TX_DONE:
                    # Clear IRQ
                    self._cmd(CMD_CLR_IRQ_STATUS, [0x03, 0xFF])
                    self.tx_count += 1
                    self.last_tx_time = time.time()
                    elapsed_ms = (time.monotonic() - start) * 1000
                    logger.info("SX1261: TX done, %d bytes in %.1f ms",
                               len(payload), elapsed_ms)
                    return True
                if irq & IRQ_TIMEOUT:
                    self._cmd(CMD_CLR_IRQ_STATUS, [0x03, 0xFF])
                    logger.warning("SX1261: TX timeout (IRQ)")
                    self.tx_errors += 1
                    return False
                time.sleep(0.001)  # 1ms poll interval

            # Timed out waiting
            logger.warning("SX1261: TX timeout (poll), %.1f s elapsed", timeout_s)
            self.tx_errors += 1
            # Abort: go back to standby
            self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            self._cmd(CMD_CLR_IRQ_STATUS, [0x03, 0xFF])
            return False

        except Exception as e:
            logger.error("SX1261: TX error: %s", e, exc_info=True)
            self.tx_errors += 1
            try:
                self._cmd(CMD_SET_STANDBY, [STDBY_RC])
                self._cmd(CMD_CLR_IRQ_STATUS, [0x03, 0xFF])
            except Exception:
                pass
            return False

    def get_status(self) -> dict:
        """Return radio status information."""
        if not self._initialized:
            return {"initialized": False}

        try:
            status = self._get_chip_status()
            errors = self._get_device_errors()
            return {
                "initialized": True,
                "chip_mode": (status >> 4) & 0x07,
                "cmd_status": (status >> 1) & 0x07,
                "device_errors": errors,
                "tx_count": self.tx_count,
                "tx_errors": self.tx_errors,
                "last_tx_time": self.last_tx_time,
                "cur_freq_hz": self._cur_freq_hz,
                "cur_bw_khz": self._cur_bw_khz,
                "cur_sf": self._cur_sf,
                "cur_cr": self._cur_cr,
                "cur_tx_power": self._cur_tx_power,
            }
        except Exception as e:
            return {"initialized": True, "error": str(e)}


    # ─── LBT / CAD / Spectrum Methods ──────────────────────────────

    def listen_before_talk(self, freq_hz: int,
                           threshold_dbm: float = -80.0,
                           timeout_ms: int = 5) -> bool:
        """Listen Before Talk: check if channel is clear.

        Sets the radio to RX mode briefly and checks RSSI against threshold.
        Returns True if channel is clear (RSSI below threshold).

        Args:
            freq_hz: Frequency to check in Hz
            threshold_dbm: RSSI threshold in dBm (default -80)
            timeout_ms: How long to listen in ms (default 5)

        Returns:
            True if channel is clear, False if busy
        """
        if not self._initialized:
            raise RuntimeError("SX1261 not initialized")

        try:
            # Go to standby
            self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            self._wait_busy()

            # Set frequency
            self._set_frequency(freq_hz)

            # Set RX mode with timeout
            # timeout_val = timeout_ms * 1000 / 15.625
            timeout_val = min(int(timeout_ms * 64), 0xFFFFFF)
            self._cmd(CMD_SET_RX, [
                (timeout_val >> 16) & 0xFF,
                (timeout_val >> 8) & 0xFF,
                timeout_val & 0xFF,
            ])

            # Wait for RX to settle
            time.sleep(timeout_ms / 1000.0 + 0.001)

            # Read instantaneous RSSI
            rssi = self._get_rssi_inst()

            # Back to standby
            self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            self._wait_busy()

            channel_clear = rssi < threshold_dbm
            logger.debug("SX1261 LBT: freq=%d RSSI=%.1f threshold=%.1f clear=%s",
                        freq_hz, rssi, threshold_dbm, channel_clear)
            return channel_clear

        except Exception as e:
            logger.error("SX1261 LBT error: %s", e)
            try:
                self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            except Exception:
                pass
            return True  # Fail-open: allow TX if LBT fails

    def channel_activity_detection(self, freq_hz: int,
                                    cad_symbol_num: int = 2,
                                    cad_det_peak: int = 22,
                                    cad_det_min: int = 10) -> bool:
        """Channel Activity Detection: check for LoRa preamble.

        Uses the SX1261 CAD engine to detect active LoRa transmissions.
        Returns True if activity detected.

        Args:
            freq_hz: Frequency to check in Hz
            cad_symbol_num: Number of symbols for CAD (1-4)
            cad_det_peak: CAD detection peak threshold
            cad_det_min: CAD detection minimum threshold

        Returns:
            True if LoRa activity detected, False if clear
        """
        if not self._initialized:
            raise RuntimeError("SX1261 not initialized")

        try:
            # Go to standby
            self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            self._wait_busy()

            # Set frequency
            self._set_frequency(freq_hz)

            # Clear IRQs
            self._cmd(CMD_CLR_IRQ_STATUS, [0x03, 0xFF])
            self._wait_busy()

            # Set CAD params
            # SetCadParams(cadSymbolNum, cadDetPeak, cadDetMin, cadExitMode, cadTimeout)
            # cadExitMode: 0x00 = CAD only, 0x01 = CAD then RX
            self._cmd(CMD_SET_CAD_PARAMS, [
                cad_symbol_num & 0xFF,
                cad_det_peak & 0xFF,
                cad_det_min & 0xFF,
                0x00,  # CAD only mode
                0x00, 0x00, 0x00,  # No timeout (CAD only)
            ])
            self._wait_busy()

            # Set IRQ for CAD done + CAD detected
            irq_mask = IRQ_CAD_DONE | IRQ_CAD_DETECTED
            self._cmd(CMD_SET_DIO_IRQ_PARAMS, [
                (irq_mask >> 8) & 0xFF, irq_mask & 0xFF,
                (irq_mask >> 8) & 0xFF, irq_mask & 0xFF,
                0x00, 0x00, 0x00, 0x00,
            ])
            self._wait_busy()

            # Start CAD
            self._cmd(CMD_SET_CAD, [])

            # Wait for CAD to complete (typically < 1ms per symbol)
            start = time.monotonic()
            while time.monotonic() - start < 0.5:  # 500ms timeout
                irq = self._get_irq_status()
                if irq & IRQ_CAD_DONE:
                    detected = bool(irq & IRQ_CAD_DETECTED)
                    self._cmd(CMD_CLR_IRQ_STATUS, [0x03, 0xFF])
                    self._cmd(CMD_SET_STANDBY, [STDBY_RC])
                    logger.debug("SX1261 CAD: freq=%d detected=%s",
                                freq_hz, detected)
                    return detected
                time.sleep(0.001)

            # Timeout
            self._cmd(CMD_CLR_IRQ_STATUS, [0x03, 0xFF])
            self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            logger.warning("SX1261 CAD: timeout at freq=%d", freq_hz)
            return False

        except Exception as e:
            logger.error("SX1261 CAD error: %s", e)
            try:
                self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            except Exception:
                pass
            return False

    def get_rssi(self, freq_hz: int, settle_ms: int = 2) -> float:
        """Get instantaneous RSSI at a specific frequency.

        Args:
            freq_hz: Frequency to measure in Hz
            settle_ms: Time to wait in RX before reading RSSI

        Returns:
            RSSI value in dBm
        """
        if not self._initialized:
            raise RuntimeError("SX1261 not initialized")

        try:
            self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            self._wait_busy()
            self._set_frequency(freq_hz)

            # Enter RX continuous
            self._cmd(CMD_SET_RX, [0xFF, 0xFF, 0xFF])  # Continuous RX
            time.sleep(settle_ms / 1000.0)

            rssi = self._get_rssi_inst()

            self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            self._wait_busy()

            return rssi

        except Exception as e:
            logger.error("SX1261 get_rssi error: %s", e)
            try:
                self._cmd(CMD_SET_STANDBY, [STDBY_RC])
            except Exception:
                pass
            return -150.0  # Return very low RSSI on error

    def get_rssi_scan(self, freq_start_hz: int, freq_end_hz: int,
                      step_hz: int = 200000,
                      settle_ms: int = 2) -> list[dict]:
        """Scan a frequency range and return RSSI at each point.

        Args:
            freq_start_hz: Start frequency in Hz
            freq_end_hz: End frequency in Hz
            step_hz: Step size in Hz (default 200kHz)
            settle_ms: Settle time per frequency in ms

        Returns:
            List of {"freq_hz": int, "rssi_dbm": float}
        """
        results = []
        freq = freq_start_hz
        while freq <= freq_end_hz:
            rssi = self.get_rssi(freq, settle_ms)
            results.append({"freq_hz": freq, "rssi_dbm": round(rssi, 1)})
            freq += step_hz
        return results

    def _get_rssi_inst(self) -> float:
        """Read instantaneous RSSI from the radio.

        Returns RSSI in dBm. Formula: RSSI = -raw_value / 2
        """
        rx = self._cmd_read(CMD_GET_RSSI_INST, 1)
        if len(rx) >= 3:
            return -rx[2] / 2.0
        return -150.0

    # ─── SPI Primitives ────────────────────────────────────────────

    def _cmd(self, opcode: int, params: list[int] | None = None) -> list[int]:
        """Send an SPI command and return the response bytes."""
        tx = [opcode]
        if params:
            tx.extend(params)
        rx = self._spi.xfer2(tx)
        return rx

    def _cmd_read(self, opcode: int, n_response: int) -> list[int]:
        """Send a command and read n_response bytes (with NOP padding)."""
        tx = [opcode] + [0x00] * (n_response + 1)  # +1 for status byte
        rx = self._spi.xfer2(tx)
        return rx  # rx[0]=status during command, rx[1]=status, rx[2:] = data

    def _write_register(self, addr: int, data: list[int]) -> None:
        """Write to one or more consecutive registers."""
        tx = [
            CMD_WRITE_REGISTER,
            (addr >> 8) & 0xFF,
            addr & 0xFF,
        ] + data
        self._spi.xfer2(tx)

    def _read_register(self, addr: int, n_bytes: int = 1) -> list[int]:
        """Read one or more consecutive registers."""
        tx = [
            CMD_READ_REGISTER,
            (addr >> 8) & 0xFF,
            addr & 0xFF,
            0x00,  # NOP (status)
        ] + [0x00] * n_bytes
        rx = self._spi.xfer2(tx)
        return rx[4:]  # Skip cmd + addr + status

    def _write_buffer(self, offset: int, data: bytes) -> None:
        """Write data to the TX buffer starting at offset."""
        tx = [CMD_WRITE_BUFFER, offset] + list(data)
        self._spi.xfer2(tx)

    # ─── Configuration Helpers ─────────────────────────────────────

    def _set_frequency(self, freq_hz: int) -> None:
        """Set RF frequency.

        freq_reg = freq_hz * 2^25 / 32e6
        """
        freq_reg = int(freq_hz * (1 << 25) / 32_000_000)
        self._cmd(CMD_SET_RF_FREQUENCY, [
            (freq_reg >> 24) & 0xFF,
            (freq_reg >> 16) & 0xFF,
            (freq_reg >> 8) & 0xFF,
            freq_reg & 0xFF,
        ])
        self._wait_busy()
        logger.debug("SX1261: freq set to %d Hz (reg=0x%08X)", freq_hz, freq_reg)

    def _set_modulation_params(self, sf: int, bw_khz: float, cr: int) -> None:
        """Set LoRa modulation parameters.

        SetModulationParams(SF, BW, CR, LowDataRateOptimize)
        LDRO is auto-enabled when symbol time > 16.38ms.
        """
        bw_reg = BW_KHZ_TO_REG.get(bw_khz)
        if bw_reg is None:
            # Try integer
            bw_reg = BW_KHZ_TO_REG.get(int(bw_khz))
        if bw_reg is None:
            raise ValueError(f"Unsupported bandwidth: {bw_khz} kHz")

        cr_reg = CR_TO_REG.get(cr)
        if cr_reg is None:
            raise ValueError(f"Unsupported coding rate: {cr} (use 5-8)")

        # Calculate if LDRO needed: symbol_time = 2^SF / BW
        # LDRO needed when symbol_time > 16.38ms
        symbol_time_ms = (2 ** sf) / (bw_khz * 1000) * 1000
        ldro = 0x01 if symbol_time_ms > 16.38 else 0x00

        self._cmd(CMD_SET_MOD_PARAMS, [sf, bw_reg, cr_reg, ldro])
        self._wait_busy()
        logger.debug("SX1261: mod params SF%d BW%s CR4/%d LDRO=%d (sym=%.2fms)",
                    sf, bw_khz, cr, ldro, symbol_time_ms)

    def _set_packet_params(
        self,
        preamble_len: int = 17,
        payload_len: int = 0,
        crc_on: bool = True,
        invert_iq: bool = False,
    ) -> None:
        """Set LoRa packet parameters.

        SetPacketParams(PreambleLen[15:8], PreambleLen[7:0],
                       HeaderType, PayloadLen, CrcType, InvertIQ)
        """
        header_type = 0x00  # Explicit header
        crc_type = 0x01 if crc_on else 0x00
        iq_setup = 0x01 if invert_iq else 0x00

        self._cmd(CMD_SET_PKT_PARAMS, [
            (preamble_len >> 8) & 0xFF,
            preamble_len & 0xFF,
            header_type,
            payload_len,
            crc_type,
            iq_setup,
        ])
        self._wait_busy()

    def _set_sync_word(self, sync_word: int) -> None:
        """Set LoRa sync word via register write.

        Public network:  0x3444
        Private network: 0x1424
        MeshCore uses:   0x1424 (private)
        """
        msb = (sync_word >> 8) & 0xFF
        lsb = sync_word & 0xFF
        self._write_register(REG_LORA_SYNC_WORD_MSB, [msb])
        self._write_register(REG_LORA_SYNC_WORD_LSB, [lsb])
        self._wait_busy()
        logger.debug("SX1261: sync word set to 0x%04X", sync_word)

    def _set_tx_power(self, power_dbm: int) -> None:
        """Set TX output power.

        SX1261: -17 to +15 dBm
        SetTxParams(power, rampTime)
        rampTime: 0x04 = 200us (good default)
        """
        power_dbm = max(-17, min(15, power_dbm))
        ramp_time = 0x04  # 200us ramp
        # Power is signed byte
        power_byte = power_dbm & 0xFF
        self._cmd(CMD_SET_TX_PARAMS, [power_byte, ramp_time])
        self._wait_busy()
        logger.debug("SX1261: TX power set to %d dBm", power_dbm)

    # ─── Status Helpers ────────────────────────────────────────────

    def _get_chip_status(self) -> int:
        """GetStatus command - returns status byte."""
        rx = self._cmd_read(CMD_GET_STATUS, 0)
        return rx[1] if len(rx) > 1 else 0

    def _get_irq_status(self) -> int:
        """GetIrqStatus - returns 16-bit IRQ flags."""
        rx = self._cmd_read(CMD_GET_IRQ_STATUS, 2)
        if len(rx) >= 4:
            return (rx[2] << 8) | rx[3]
        return 0

    def _get_device_errors(self) -> int:
        """GetDeviceErrors - returns 16-bit error flags."""
        rx = self._cmd_read(CMD_GET_DEVICE_ERRORS, 2)
        if len(rx) >= 4:
            return (rx[2] << 8) | rx[3]
        return 0

    def _wait_busy(self, timeout: float = 0.5) -> None:
        """Wait for the chip to be ready by polling GetStatus.

        The SX1261 BUSY pin is not easily accessible on the WM1303 hat
        (no dedicated GPIO), so we poll via SPI status instead.
        Chip mode 0x2 = STDBY_RC, 0x3 = STDBY_XOSC (both = ready)
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            status = self._get_chip_status()
            cmd_status = (status >> 1) & 0x07
            # cmd_status: 0x2 = data available, 0x3 = cmd timeout,
            #             0x4 = cmd processing error, 0x5 = failure to execute,
            #             0x6 = cmd tx done
            # If we get status != 0, chip is responding
            if status != 0x00 and cmd_status not in (0x04, 0x05):
                return
            time.sleep(0.001)
        logger.warning("SX1261: wait_busy timeout after %.1fs", timeout)

    # ─── Hardware Reset (sysfs GPIO, matching reset_lgw.sh) ────────

    # RPi4 sysfs GPIO base offset: BCM pin + 512 = sysfs number
    SYSFS_GPIO_BASE = 512

    def _hw_reset(self) -> None:
        """Reset SX1261 via sysfs GPIO (like reset_lgw.sh does).

        Uses /sys/class/gpio interface with BCM pin + 512 offset.
        If reset fails (GPIO busy/permissions), skip it since
        reset_lgw.sh already resets the SX1261 at lora_pkt_fwd start.
        """
        sysfs_pin = self._reset_gpio + self.SYSFS_GPIO_BASE
        gpio_path = f"/sys/class/gpio/gpio{sysfs_pin}"

        try:
            self._reset_via_sysfs(sysfs_pin, gpio_path)
            return
        except Exception as e:
            logger.warning("SX1261: sysfs reset GPIO%d failed: %s", sysfs_pin, e)

        # Fallback: skip reset, rely on reset_lgw.sh having already done it
        logger.info("SX1261: skipping HW reset (reset_lgw.sh already resets SX1261)")

    def _reset_via_sysfs(self, sysfs_pin: int, gpio_path: str) -> None:
        """Reset using /sys/class/gpio sysfs interface."""
        import subprocess

        # Export GPIO if not already exported
        if not os.path.exists(gpio_path):
            try:
                with open("/sys/class/gpio/export", "w") as f:
                    f.write(str(sysfs_pin))
                time.sleep(0.1)
            except (PermissionError, OSError):
                subprocess.run(
                    ["sudo", "sh", "-c", f"echo {sysfs_pin} > /sys/class/gpio/export"],
                    check=True, timeout=5
                )
                time.sleep(0.1)

        # Set direction to output
        self._sysfs_write(f"{gpio_path}/direction", "out")
        time.sleep(0.05)

        # Assert reset (low)
        self._sysfs_write(f"{gpio_path}/value", "0")
        time.sleep(0.1)

        # Release reset (high)
        self._sysfs_write(f"{gpio_path}/value", "1")
        time.sleep(0.1)
        logger.info("SX1261: hardware reset via sysfs GPIO%d (BCM%d)", sysfs_pin, self._reset_gpio)

    @staticmethod
    def _sysfs_write(path: str, value: str) -> None:
        """Write a value to a sysfs file, using sudo if needed."""
        import subprocess
        try:
            with open(path, "w") as f:
                f.write(value)
        except PermissionError:
            subprocess.run(
                ["sudo", "sh", "-c", f"echo {value} > {path}"],
                check=True, timeout=5
            )
