/*
 / _____)             _              | |
( (____  _____ ____ _| |_ _____  ____| |__
 \____ \| ___ |    (_   _) ___ |/ ___)  _ \
 _____) ) ____| | | || |_| ____( (___| | | |
(______/|_____)_|_|_| \__)_____)\____)_| |_|
  (C)2019 Semtech

Description:
    Functions used to handle LoRa concentrator SX1261 radio used to handle LBT,
    Spectral Scan, CAD and LoRa RX (Channel E).

License: Revised BSD License, see LICENSE.TXT file include in the project
*/


/*
 * ----------------------------------------------------------------------
 * pyMC_WM1303 — WM1303 Repeater adaptations
 * ----------------------------------------------------------------------
 * Copyright (c) 2026 HansvanMeer  (GitHub: @HansvanMeer)
 *
 * Licensed under the PolyForm Noncommercial License 1.0.0.
 * See LICENSE and COMMERCIAL.md in the pyMC_WM1303 repository:
 *   https://github.com/HansvanMeer/pyMC_WM1303
 *
 * Any portions of this file derived from Semtech's sx1302_hal remain
 * under Semtech's Revised BSD License (where applicable, see header
 * above). Modifications and original additions in this file are
 * licensed under PolyForm Noncommercial 1.0.0.
 *
 * Commercial use is NOT permitted without a separate written agreement.
 * See COMMERCIAL.md for commercial licensing inquiries.
 * ----------------------------------------------------------------------
 */

/* -------------------------------------------------------------------------- */
/* --- DEPENDANCIES --------------------------------------------------------- */

#include <stdint.h>     /* C99 types */
#include <stdio.h>      /* printf fprintf */
#include <string.h>     /* strncmp */
#include <fcntl.h>      /* open */
#include <unistd.h>     /* write, close */

#include "loragw_sx1261.h"
#include "loragw_spi.h"
#include "loragw_com.h"
#include "loragw_aux.h"
#include "loragw_reg.h"
#include "loragw_hal.h"

#include "sx1261_com.h"

#include "sx1261_pram.var"

/* -------------------------------------------------------------------------- */
/* --- PRIVATE MACROS ------------------------------------------------------- */

#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#if DEBUG_LBT == 1
    #define DEBUG_MSG(str)                fprintf(stdout, str)
    #define DEBUG_PRINTF(fmt, args...)    fprintf(stdout,"%s:%d: "fmt, __FUNCTION__, __LINE__, args)
    #define CHECK_NULL(a)                if(a==NULL){fprintf(stderr,"%s:%d: ERROR: NULL POINTER AS ARGUMENT\n", __FUNCTION__, __LINE__);return LGW_REG_ERROR;}
#else
    #define DEBUG_MSG(str)
    #define DEBUG_PRINTF(fmt, args...)
    #define CHECK_NULL(a)                if(a==NULL){return LGW_REG_ERROR;}
#endif

#define CHECK_ERR(a)                    if(a==-1){return LGW_REG_ERROR;}

#define DEBUG_SX1261_GET_STATUS 0

/* -------------------------------------------------------------------------- */
/* --- PRIVATE CONSTANTS ---------------------------------------------------- */

#define SX1261_PRAM_VERSION_FULL_SIZE 16 /* 15 bytes + terminating char */

/* SX126x IRQ bit masks (standard from datasheet) */
#define SX1261_IRQ_TX_DONE              0x0001  /* bit 0 */
#define SX1261_IRQ_RX_DONE              0x0002  /* bit 1 */
#define SX1261_IRQ_PREAMBLE_DETECTED    0x0004  /* bit 2 */
#define SX1261_IRQ_SYNC_WORD_VALID      0x0008  /* bit 3 (FSK only) */
#define SX1261_IRQ_HEADER_VALID         0x0010  /* bit 4 */
#define SX1261_IRQ_HEADER_ERR           0x0020  /* bit 5 */
#define SX1261_IRQ_CRC_ERR              0x0040  /* bit 6 */
#define SX1261_IRQ_TIMEOUT              0x0200  /* bit 9 */

/* SX126x LoRa sync word registers */
#define SX1261_REG_LORA_SYNC_WORD_MSB   0x0740
#define SX1261_REG_LORA_SYNC_WORD_LSB   0x0741

/* SX126x workaround registers */
#define SX1261_REG_BW500_WORKAROUND     0x0889  /* BW < 500 kHz optimization (datasheet 15.1) */
#define SX1261_REG_IQ_POLARITY          0x0736  /* IQ polarity fix (datasheet 15.4) */
#define SX1261_REG_RX_GAIN              0x029F  /* RX gain: 0x00=power saving, 0x01=boosted */

/* SetDIO3AsTCXOCtrl opcode (not in original sx1261_defs.h enum) */
#define SX1261_OP_SET_DIO3_AS_TCXO_CTRL 0x97

/* -------------------------------------------------------------------------- */
/* --- PRIVATE VARIABLES ---------------------------------------------------- */

/* LoRa RX (Channel E) state */
static bool sx1261_lora_rx_enabled = false;
static uint32_t sx1261_lora_rx_freq = 0;
static uint8_t sx1261_lora_rx_bw = 0;
static uint8_t sx1261_lora_rx_sf = 0;
static uint8_t sx1261_lora_rx_cr = 0;
static bool sx1261_lora_rx_boosted = true; /* default: boosted LNA for max sensitivity */

/* TX inhibit flag: when true, prevents automatic LoRa RX restart.
   Used during the CAD → lgw_send window to keep the SX1261 in STDBY
   so the FEM stays in a neutral state and doesn't route to LNA (RX path)
   which would prevent the SX1302 PA from activating for TX.
   Channel E RX resumes immediately after lgw_send completes. */
static volatile bool sx1261_tx_inhibit_rx = false;
/* WM1303: deferred RX restart flag — set when spectral scan abort finds
   tx_inhibit active. Checked and cleared when tx_inhibit is released. */
static volatile bool sx1261_deferred_rx_restart = false;

/* Last measured LBT RSSI (dBm). Updated in sx1261_lbt_start() after the
   scan period. Readable via sx1261_lbt_get_last_rssi(). */
static int16_t sx1261_lbt_last_rssi = -128;

/* -------------------------------------------------------------------------- */
/* --- PRIVATE FUNCTIONS ---------------------------------------------------- */

int sx1261_pram_get_version(char * version_str) {
    uint8_t buff[3 + SX1261_PRAM_VERSION_FULL_SIZE] = { 0 };
    int x;

    /* Check input parameter */
    CHECK_NULL(version_str);

    /* Get version string (15 bytes) at address 0x320 */
    buff[0] = 0x03;
    buff[1] = 0x20;
    buff[2] = 0x00; /* status */
    x = sx1261_reg_r(SX1261_READ_REGISTER, buff, 18);
    if (x != LGW_REG_SUCCESS) {
        printf("ERROR: failed to read SX1261 PRAM version\n");
        return x;
    }

    /* Return full PRAM version string */
    buff[18] = '\0';
    strncpy(version_str, (char*)(buff + 3), 16); /* 15 bytes + terminating char */
    version_str[16] = '\0';

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_get_status(uint8_t * status) {
    uint8_t buff[1];

    buff[0] = 0x00;
    sx1261_reg_r(SX1261_GET_STATUS, buff, 1);

    *status = buff[0] & 0x7E; /* ignore bit 0 & 7 */

    DEBUG_PRINTF("SX1261: %s: get_status: 0x%02X (0x%02X)\n", __FUNCTION__, *status, buff[0]);

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_check_status(uint8_t expected_status) {
    int err;
    uint8_t status;

    err = sx1261_get_status(&status);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to get status\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    if (status != expected_status) {
        printf("ERROR: %s: SX1261 status is not as expected: got:0x%02X expected:0x%02X\n", __FUNCTION__, status, expected_status);
        return LGW_REG_ERROR;
    }

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

const char * get_scan_status_str(const lgw_spectral_scan_status_t status) {
    switch (status) {
        case LGW_SPECTRAL_SCAN_STATUS_NONE:
            return "LGW_SPECTRAL_SCAN_STATUS_NONE";
        case LGW_SPECTRAL_SCAN_STATUS_ON_GOING:
            return "LGW_SPECTRAL_SCAN_STATUS_ON_GOING";
        case LGW_SPECTRAL_SCAN_STATUS_ABORTED:
            return "LGW_SPECTRAL_SCAN_STATUS_ABORTED";
        case LGW_SPECTRAL_SCAN_STATUS_COMPLETED:
            return "LGW_SPECTRAL_SCAN_STATUS_COMPLETED";
        default:
            return "LGW_SPECTRAL_SCAN_STATUS_UNKNOWN";
    }
}

/* -------------------------------------------------------------------------- */
/* --- PUBLIC FUNCTIONS DEFINITION ------------------------------------------ */

int sx1261_connect(lgw_com_type_t com_type, const char *com_path) {
    if (com_type == LGW_COM_SPI && com_path == NULL) {
        printf("ERROR: %s: unspecified COM path to connect to sx1261 radio\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }
    return sx1261_com_open(com_type, com_path);
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_disconnect(void) {
    return sx1261_com_close();
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_reg_w(sx1261_op_code_t op_code, uint8_t *data, uint16_t size) {
    int com_stat;

    /* checking input parameters */
    CHECK_NULL(data);

    com_stat = sx1261_com_w(op_code, data, size);
    if (com_stat != LGW_COM_SUCCESS) {
        printf("ERROR: COM ERROR DURING SX1261 RADIO REGISTER WRITE\n");
        return LGW_REG_ERROR;
    } else {
        return LGW_REG_SUCCESS;
    }
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_reg_r(sx1261_op_code_t op_code, uint8_t *data, uint16_t size) {
    int com_stat;

    /* checking input parameters */
    CHECK_NULL(data);

    com_stat = sx1261_com_r(op_code, data, size);
    if (com_stat != LGW_COM_SUCCESS) {
        printf("ERROR: COM ERROR DURING SX1261 RADIO REGISTER READ\n");
        return LGW_REG_ERROR;
    } else {
        return LGW_REG_SUCCESS;
    }
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_load_pram(void) {
    int i, err;
    uint8_t buff[32];
    char pram_version[SX1261_PRAM_VERSION_FULL_SIZE];

    /* Bulk PRAM buffer: 2 bytes address + 386*4 bytes data = 1546 bytes */
    static uint8_t pram_bulk[2 + PRAM_COUNT * 4];
    static int pram_bulk_ready = 0;

    /* Pre-build the bulk buffer once (start address + all PRAM data MSB-first) */
    if (!pram_bulk_ready) {
        pram_bulk[0] = 0x80; /* Start address MSB: 0x8000 */
        pram_bulk[1] = 0x00; /* Start address LSB */
        for (i = 0; i < (int)PRAM_COUNT; i++) {
            uint32_t val = pram[i];
            pram_bulk[2 + i*4 + 0] = (val >> 24) & 0xFF;
            pram_bulk[2 + i*4 + 1] = (val >> 16) & 0xFF;
            pram_bulk[2 + i*4 + 2] = (val >>  8) & 0xFF;
            pram_bulk[2 + i*4 + 3] = (val >>  0) & 0xFF;
        }
        pram_bulk_ready = 1;
    }

    /* Set Radio in Standby mode */
    buff[0] = (uint8_t)SX1261_STDBY_RC;
    sx1261_reg_w(SX1261_SET_STANDBY, buff, 1);

    /* Check status */
    err = sx1261_check_status(SX1261_STATUS_MODE_STBY_RC | SX1261_STATUS_READY);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: SX1261 status error\n", __FUNCTION__);
        return -1;
    }

    err = sx1261_pram_get_version(pram_version);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: SX1261 failed to get pram version\n", __FUNCTION__);
        return -1;
    }
    printf("SX1261: PRAM version: %s\n", pram_version);

    /* Enable patch update */
    buff[0] = 0x06;
    buff[1] = 0x10;
    buff[2] = 0x10;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    CHECK_ERR(err);

    /* Load patch — single bulk SPI write instead of 386 individual writes */
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, pram_bulk, 2 + PRAM_COUNT * 4);
    CHECK_ERR(err);

    /* Disable patch update */
    buff[0] = 0x06;
    buff[1] = 0x10;
    buff[2] = 0x00;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    CHECK_ERR(err);

    /* Update pram */
    buff[0] = 0;
    err = sx1261_reg_w(0xd9, buff, 0);
    CHECK_ERR(err);

    err = sx1261_pram_get_version(pram_version);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: SX1261 failed to get pram version\n", __FUNCTION__);
        return -1;
    }
    printf("SX1261: PRAM version: %s\n", pram_version);

    /* Check PRAM version (only last 4 bytes) */
    if (strncmp(pram_version + 11, sx1261_pram_version_string, 4) != 0) {
        printf("ERROR: SX1261 PRAM version mismatch (got:%s expected:%s)\n", pram_version + 11, sx1261_pram_version_string);
        return -1;
    }

    return 0;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_calibrate(uint32_t freq_hz) {
    int err = LGW_REG_SUCCESS;
    uint8_t buff[16];

    buff[0] = 0x00;
    err = sx1261_reg_r(SX1261_GET_STATUS, buff, 1);
    CHECK_ERR(err);

    /* Run calibration */
    if ((freq_hz > 430E6) && (freq_hz < 440E6)) {
        buff[0] = 0x6B;
        buff[1] = 0x6F;
    } else if ((freq_hz > 470E6) && (freq_hz < 510E6)) {
        buff[0] = 0x75;
        buff[1] = 0x81;
    } else if ((freq_hz > 779E6) && (freq_hz < 787E6)) {
        buff[0] = 0xC1;
        buff[1] = 0xC5;
    } else if ((freq_hz > 863E6) && (freq_hz < 870E6)) {
        buff[0] = 0xD7;
        buff[1] = 0xDB;
    } else if ((freq_hz > 902E6) && (freq_hz < 928E6)) {
        buff[0] = 0xE1;
        buff[1] = 0xE9;
    } else {
        printf("ERROR: failed to calibrate sx1261 radio, frequency range not supported (%u)\n", freq_hz);
        return LGW_REG_ERROR;
    }
    err = sx1261_reg_w(SX1261_CALIBRATE_IMAGE, buff, 2);
    CHECK_ERR(err);

    /* Wait for calibration to complete */
    wait_ms(4); /* Image cal wait (was 10ms, datasheet max 3.5ms) */

    buff[0] = 0x00;
    buff[1] = 0x00;
    buff[2] = 0x00;
    err = sx1261_reg_r(SX1261_GET_DEVICE_ERRORS, buff, 3);
    CHECK_ERR(err);
    if (TAKE_N_BITS_FROM(buff[2], 4, 1) != 0) {
        printf("ERROR: sx1261 Image Calibration Error\n");
        return LGW_REG_ERROR;
    }

    return err;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_setup(void) {
    int err;
    uint8_t buff[32];

    /* Set Radio in Standby mode */
    buff[0] = (uint8_t)SX1261_STDBY_RC;
    err = sx1261_reg_w(SX1261_SET_STANDBY, buff, 1);
    CHECK_ERR(err);

    /* Check radio status */
    err = sx1261_check_status(SX1261_STATUS_MODE_STBY_RC | SX1261_STATUS_READY);
    CHECK_ERR(err);

    /* Set Buffer Base address */
    buff[0] = 0x80;
    buff[1] = 0x80;
    err = sx1261_reg_w(SX1261_SET_BUFFER_BASE_ADDRESS, buff, 2);
    CHECK_ERR(err);

    /* sensi adjust */
    buff[0] = 0x08;
    buff[1] = 0xAC;
    buff[2] = 0xCB;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    CHECK_ERR(err);

    DEBUG_MSG("SX1261: setup for LBT / Spectral Scan done\n");

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_set_rx_params(uint32_t freq_hz, uint8_t bandwidth) {
    int err;
    uint8_t buff[16];
    int32_t freq_reg;
    uint8_t fsk_bw_reg;
    /* performances variables */
    struct timeval tm;

    /* Record function start time */
    _meas_time_start(&tm);

    /* Stop LoRa RX if active (will be restarted after LBT/scan completes) */
    if (sx1261_lora_rx_enabled) {
        sx1261_lora_rx_stop();
    }

    /* Set SPI write bulk mode to optimize speed on USB */
    err = sx1261_com_set_write_mode(LGW_COM_WRITE_MODE_BULK);
    CHECK_ERR(err);

    /* Disable any on-going spectral scan to free the sx1261 radio for LBT */
    err = sx1261_spectral_scan_abort();
    CHECK_ERR(err);

    /* Set FS */
    err = sx1261_reg_w(SX1261_SET_FS, buff, 0);
    CHECK_ERR(err);

#if DEBUG_SX1261_GET_STATUS /* need to disable spi bulk mode if enable this check */
    /* Check radio status */
    err = sx1261_check_status(SX1261_STATUS_MODE_FS | SX1261_STATUS_READY);
    CHECK_ERR(err);
#endif

    /* Set frequency */
    freq_reg = SX1261_FREQ_TO_REG(freq_hz);
    buff[0] = (uint8_t)(freq_reg >> 24);
    buff[1] = (uint8_t)(freq_reg >> 16);
    buff[2] = (uint8_t)(freq_reg >> 8);
    buff[3] = (uint8_t)(freq_reg >> 0);
    err = sx1261_reg_w(SX1261_SET_RF_FREQUENCY, buff, 4);
    CHECK_ERR(err);

    /* Configure RSSI averaging window */
    buff[0] = 0x08;
    buff[1] = 0x9B;
    buff[2] = 0x05 << 2;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    CHECK_ERR(err);

    /* Set PacketType */
    buff[0] = 0x00; /* FSK */
    err = sx1261_reg_w(SX1261_SET_PACKET_TYPE, buff, 1);
    CHECK_ERR(err);

    /* Set GFSK bandwidth - the GFSK RX BW is set to ~2x the LoRa BW
       to ensure the RSSI measurement covers the full LoRa channel */
    switch (bandwidth) {
        case BW_62K5HZ:
            fsk_bw_reg = 0x0B; /* RX_BW_117300 Hz - covers 62.5 kHz LoRa channel */
            break;
        case BW_125KHZ:
            fsk_bw_reg = 0x0A; /* RX_BW_234300 Hz - covers 125 kHz LoRa channel */
            break;
        case BW_250KHZ:
            fsk_bw_reg = 0x09; /* RX_BW_467000 Hz - covers 250 kHz LoRa channel */
            break;
        default:
            printf("ERROR: %s: Cannot configure sx1261 for bandwidth %u\n", __FUNCTION__, bandwidth);
            return LGW_REG_ERROR;
    }

    /* Set modulation params for FSK */
    buff[0] = 0;    // BR
    buff[1] = 0x14; // BR
    buff[2] = 0x00; // BR
    buff[3] = 0x00; // Gaussian BT disabled
    buff[4] = fsk_bw_reg;
    buff[5] = 0x02; // FDEV
    buff[6] = 0xE9; // FDEV
    buff[7] = 0x0F; // FDEV
    err = sx1261_reg_w(SX1261_SET_MODULATION_PARAMS, buff, 8);
    CHECK_ERR(err);

    /* Set packet params for FSK */
    buff[0] = 0x00; /* Preamble length MSB */
    buff[1] = 0x20; /* Preamble length LSB 32 bits*/
    buff[2] = 0x05; /* Preamble detector lenght 16 bits */
    buff[3] = 0x20; /* SyncWordLength 32 bits*/
    buff[4] = 0x00; /* AddrComp disabled */
    buff[5] = 0x01; /* PacketType variable size */
    buff[6] = 0xff; /* PayloadLength 255 bytes */
    buff[7] = 0x00; /* CRCType 1 Byte */
    buff[8] = 0x00; /* Whitening disabled*/
    err = sx1261_reg_w(SX1261_SET_PACKET_PARAMS, buff, 9);
    CHECK_ERR(err);

    /* Set Radio in Rx continuous mode */
    buff[0] = 0xFF;
    buff[1] = 0xFF;
    buff[2] = 0xFF;
    err = sx1261_reg_w(SX1261_SET_RX, buff, 3);
    CHECK_ERR(err);

    /* Flush write (USB BULK mode) */
    err = sx1261_com_flush();
    if (err != 0) {
        printf("ERROR: %s: Failed to flush sx1261 SPI\n", __FUNCTION__);
        return -1;
    }

    /* Setting back to SINGLE BULK write mode */
    err = sx1261_com_set_write_mode(LGW_COM_WRITE_MODE_SINGLE);
    CHECK_ERR(err);

#if DEBUG_SX1261_GET_STATUS
    /* Check radio status */
    err = sx1261_check_status(SX1261_STATUS_MODE_RX | SX1261_STATUS_READY);
    CHECK_ERR(err);
#endif

    DEBUG_PRINTF("SX1261: RX params set to %u Hz (bw:0x%02X)\n", freq_hz, bandwidth);

    _meas_time_stop(4, tm, __FUNCTION__);

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_lbt_start(lgw_lbt_scan_time_t scan_time_us, int8_t threshold_dbm) {
    int err;
    uint8_t buff[16];
    uint16_t nb_scan;
    uint8_t threshold_reg = -2 * threshold_dbm;
    /* performances variables */
    struct timeval tm;

    /* Record function start time */
    _meas_time_start(&tm);

    switch (scan_time_us) {
        case LGW_LBT_SCAN_TIME_128_US:
            nb_scan = 24;
            break;
        case LGW_LBT_SCAN_TIME_5000_US:
            nb_scan = 715;
            break;
        default:
            printf("ERROR: wrong scan_time_us value\n");
            return -1;
    }

#if DEBUG_SX1261_GET_STATUS
    /* Check radio status */
    err = sx1261_check_status(SX1261_STATUS_MODE_RX | SX1261_STATUS_READY);
    CHECK_ERR(err);
#endif

    /* ---- Pre-LBT noise floor RSSI (while SX1261 is in FSK RX) ---- */
    /* Read RSSI BEFORE starting the carrier sense scan, because after the
       scan completes the SX1261 may be in an undefined state where
       GetRssiInst returns sentinel values (-128/-127). */
    sx1261_lbt_last_rssi = -128;
    wait_ms(1); /* let FSK RX RSSI settle */
    err = sx1261_get_rssi_inst(&sx1261_lbt_last_rssi);
    if (err == LGW_REG_SUCCESS && sx1261_lbt_last_rssi > -128) {
        printf("INFO: [LBT] pre-scan RSSI: %d dBm (threshold: %d dBm)\n",
               sx1261_lbt_last_rssi, threshold_dbm);
    }

    /* Configure and start LBT carrier sense scan */
    buff[0] = 11; // intervall_rssi_read (10 => 7.68 usec,11 => 8.2 usec, 12 => 8.68 usec)
    buff[1] = (nb_scan >> 8) & 0xFF;
    buff[2] = (nb_scan >> 0) & 0xFF;
    buff[3] = threshold_reg;
    buff[4] = 1; // gpioId
    err = sx1261_reg_w(0x9a, buff, 5);
    CHECK_ERR(err);

    /* Wait for Scan Time before TX trigger request */
    wait_us((uint16_t)scan_time_us);

    DEBUG_PRINTF("SX1261: LBT started: scan time = %uus, threshold = %ddBm\n", (uint16_t)scan_time_us, threshold_dbm);

    _meas_time_stop(4, tm, __FUNCTION__);

    return LGW_REG_SUCCESS;

}


/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_lbt_stop(void) {
    int err;
    uint8_t buff[16];
    /* performances variables */
    struct timeval tm;

    /* Record function start time */
    _meas_time_start(&tm);

    /* Disable LBT */
    buff[0] = 0x08;
    buff[1] = 0x9B;
    buff[2] = 0x00;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    CHECK_ERR(err);

    /* Set FS */
    err = sx1261_reg_w(SX1261_SET_FS, buff, 0);
    CHECK_ERR(err);

    DEBUG_MSG("SX1261: LBT stopped\n");

    _meas_time_stop(4, tm, __FUNCTION__);

    /* Restart LoRa RX if it was configured and not inhibited by TX */
    if (sx1261_lora_rx_enabled && !sx1261_tx_inhibit_rx) {
        printf("SX1261: Restarting LoRa RX after LBT (light)\n");
        sx1261_lora_rx_restart_light();
    } else if (sx1261_lora_rx_enabled && sx1261_tx_inhibit_rx) {
        /* TX inhibit active — defer RX restart until inhibit clears */
        printf("SX1261: Deferred LoRa RX restart (TX inhibit active)\n");
        sx1261_deferred_rx_restart = true;
    }

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_lbt_get_last_rssi(int16_t *rssi) {
    if (rssi == NULL) {
        return -1;
    }
    *rssi = sx1261_lbt_last_rssi;
    return 0;
}


int sx1261_spectral_scan_start(uint16_t nb_scan) {
    int err;
    uint8_t buff[4]; /* 66 bytes for spectral scan results + 2 bytes register address + 1 dummy byte for reading */
    /* performances variables */
    struct timeval tm;

    /* Record function start time */
    _meas_time_start(&tm);

    /* Start spectral scan */
    buff[0] = (nb_scan >> 8) & 0xFF; /* nb_scan MSB */
    buff[1] = (nb_scan >> 0) & 0xFF; /* nb_scan LSB */
    buff[2] = 11; /* interval between scans - 8.2 us */
    /* Write exactly 3 bytes: nb_scan[15:0] + interval.
     * Using 9 here overruns buff[4] and corrupts stack state. */
    err = sx1261_reg_w(0x9b, buff, 3);
    CHECK_ERR(err);

    DEBUG_MSG("INFO: Spectral Scan started...\n");

    _meas_time_stop(4, tm, __FUNCTION__);

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_spectral_scan_get_results(int8_t rssi_offset, int16_t * levels_dbm, uint16_t * results) {
    int err, i;
    uint8_t buff[69]; /* 66 bytes for spectral scan results + 2 bytes register address + 1 dummy byte for reading */
    /* performances variables */
    struct timeval tm;

    /* Record function start time */
    _meas_time_start(&tm);

    /* Check input parameters */
    CHECK_NULL(levels_dbm);
    CHECK_NULL(results);

    /* Get the results (66 bytes) */
    buff[0] = 0x04;
    buff[1] = 0x01;
    buff[2] = 0x00; /* dummy */
    for (i = 3; i < (66 + 3) ; i++) {
        buff[i] = 0x00;
    }
    err = sx1261_reg_r(SX1261_READ_REGISTER, buff, 66 + 3);
    CHECK_ERR(err);

    /* Copy the results in the given buffers */
    /* The number of points measured ABOVE each threshold */
    for (i = 0; i < 32; i++) {
        levels_dbm[i] = -i*4 + rssi_offset;
        results[i] = (uint16_t)((buff[3 + i*2] << 8) | buff[3 + i*2 + 1]);
    }
    /* The number of points measured BELOW the lower threshold */
    levels_dbm[32] = -31*4 + rssi_offset;
    results[32] = (uint16_t)((buff[3 + 32*2] << 8) + buff[3 + 32*2 + 1]);

    _meas_time_stop(4, tm, __FUNCTION__);

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_spectral_scan_status(lgw_spectral_scan_status_t * status) {
    int err;
    uint8_t buff[16];
    /* performances variables */
    struct timeval tm;

    CHECK_NULL(status);

    /* Record function start time */
    _meas_time_start(&tm);

    /* Get status */
    buff[0] = 0x07;
    buff[1] = 0xCD;
    buff[2] = 0x00; /* dummy */
    buff[3] = 0x00; /* read value holder */
    err = sx1261_reg_r(SX1261_READ_REGISTER, buff, 4);
    CHECK_ERR(err);

    switch (buff[3]) {
        case 0x00:
            *status = LGW_SPECTRAL_SCAN_STATUS_NONE;
            break;
        case 0x0F:
            *status = LGW_SPECTRAL_SCAN_STATUS_ON_GOING;
            break;
        case 0xF0:
            *status = LGW_SPECTRAL_SCAN_STATUS_ABORTED;
            break;
        case 0xFF:
            *status = LGW_SPECTRAL_SCAN_STATUS_COMPLETED;
            break;
        default:
            *status = LGW_SPECTRAL_SCAN_STATUS_UNKNOWN;
            break;
    }

    DEBUG_PRINTF("INFO: %s: %s\n", __FUNCTION__, get_scan_status_str(*status));

    _meas_time_stop(4, tm, __FUNCTION__);

    return LGW_REG_SUCCESS;
}


/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

/**
 * @brief Lightweight LoRa RX restart after spectral scan.
 *
 * After a spectral scan, the SX1261 is in FSK mode but TCXO, DC-DC, and
 * calibration are still valid. This function only switches back to LoRa mode
 * and reconfigures modulation/packet params, avoiding the heavy 22ms+
 * re-initialization that would block the capture thread.
 *
 * Total execution time: ~3-5ms (no wait_ms calls).
 */
int sx1261_lora_rx_restart_light(void) {
    int err;
    uint8_t buff[16];
    int32_t freq_reg;

    if (!sx1261_lora_rx_enabled) {
        return LGW_REG_SUCCESS; /* nothing to restart */
    }

    if (sx1261_tx_inhibit_rx) {
        printf("SX1261: LoRa RX restart INHIBITED (TX in progress)\n");
        return LGW_REG_SUCCESS; /* TX window active, don't restart RX */
    }

    printf("SX1261: LoRa RX light restart (post-scan)\n");

    /* Use bulk write mode for speed */
    err = sx1261_com_set_write_mode(LGW_COM_WRITE_MODE_BULK);
    if (err != LGW_REG_SUCCESS) return err;

    /* Step 1: Set Standby XOSC (crystal already running from scan) */
    buff[0] = 0x01; /* STDBY_XOSC */
    err = sx1261_reg_w(SX1261_SET_STANDBY, buff, 1);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Step 2: Set packet type to LoRa (was FSK during scan) */
    buff[0] = 0x01; /* LoRa */
    err = sx1261_reg_w(SX1261_SET_PACKET_TYPE, buff, 1);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Step 3: Set RF frequency */
    freq_reg = SX1261_FREQ_TO_REG(sx1261_lora_rx_freq);
    buff[0] = (uint8_t)(freq_reg >> 24);
    buff[1] = (uint8_t)(freq_reg >> 16);
    buff[2] = (uint8_t)(freq_reg >> 8);
    buff[3] = (uint8_t)(freq_reg >> 0);
    err = sx1261_reg_w(SX1261_SET_RF_FREQUENCY, buff, 4);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Step 4: Set modulation params (SF, BW, CR, LowDataRateOpt) */
    buff[0] = sx1261_lora_rx_sf;  /* SF */
    buff[1] = sx1261_lora_rx_bw;  /* BW */
    buff[2] = sx1261_lora_rx_cr;  /* CR */
    buff[3] = 0x00;               /* LowDataRateOptimize off */
    err = sx1261_reg_w(SX1261_SET_MODULATION_PARAMS, buff, 4);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Step 5: Set packet params */
    buff[0] = 0x00; buff[1] = 0x08; /* PreambleLength = 8 */
    buff[2] = 0x00;                 /* HeaderType: explicit */
    buff[3] = 0xFF;                 /* PayloadLength: max */
    buff[4] = 0x01;                 /* CRC on */
    buff[5] = 0x00;                 /* Standard IQ */
    err = sx1261_reg_w(SX1261_SET_PACKET_PARAMS, buff, 6);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Step 6: Set sync word for LoRa */
    buff[0] = 0x07; buff[1] = 0x40;
    buff[2] = 0x14;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    buff[0] = 0x07; buff[1] = 0x44;
    buff[2] = 0x24;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Step 7: IQ polarity fix (required for standard IQ) */
    {
        uint8_t rb[4];
        rb[0] = 0x07; rb[1] = 0x36; rb[2] = 0x00;
        err = sx1261_reg_r(SX1261_READ_REGISTER, rb, 4);
        if (err != LGW_REG_SUCCESS) goto cleanup;
        buff[0] = 0x07; buff[1] = 0x36;
        buff[2] = rb[3] | 0x04;  /* Set bit 2 for standard IQ */
        err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
        if (err != LGW_REG_SUCCESS) goto cleanup;
    }

    /* Step 8: Boosted LNA RX gain */
    buff[0] = 0x08; buff[1] = 0xAC;
    buff[2] = 0x96;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Step 9: Set buffer base address */
    buff[0] = 0x00; buff[1] = 0x00;
    err = sx1261_reg_w(SX1261_SET_BUFFER_BASE_ADDRESS, buff, 2);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Step 10: Set DIO/IRQ params - enable RxDone, Timeout, CRC error */
    buff[0] = 0x00; buff[1] = 0x62;  /* IRQ mask: RxDone | Timeout | CRCErr */
    buff[2] = 0x00; buff[3] = 0x02;  /* DIO1: RxDone */
    buff[4] = 0x00; buff[5] = 0x00;  /* DIO2: none */
    buff[6] = 0x00; buff[7] = 0x00;  /* DIO3: none */
    err = sx1261_reg_w(SX1261_SET_DIO_IRQ_PARAMS, buff, 8);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Step 11: Clear all IRQs */
    buff[0] = 0xFF; buff[1] = 0xFF;
    err = sx1261_reg_w(SX1261_CLR_IRQ_STATUS, buff, 2);
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Flush bulk writes */
    err = sx1261_com_flush();
    if (err != LGW_REG_SUCCESS) goto cleanup;

    /* Step 12: Start continuous RX (single write, not bulk) */
    err = sx1261_com_set_write_mode(LGW_COM_WRITE_MODE_SINGLE);
    if (err != LGW_REG_SUCCESS) return err;

    buff[0] = 0xFF; buff[1] = 0xFF; buff[2] = 0xFF; /* continuous */
    err = sx1261_reg_w(SX1261_SET_RX, buff, 3);
    if (err != LGW_REG_SUCCESS) return err;

    printf("SX1261: LoRa RX light restart complete (no calibration)\n");
    return LGW_REG_SUCCESS;

cleanup:
    sx1261_com_set_write_mode(LGW_COM_WRITE_MODE_SINGLE);
    printf("ERROR: sx1261_lora_rx_restart_light failed\n");
    return LGW_REG_ERROR;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

int sx1261_spectral_scan_abort(void) {
    int err;
    uint8_t buff[16];
    /* performances variables */
    struct timeval tm;

    /* Record function start time */
    _meas_time_start(&tm);

    /* Disable LBT */
    buff[0] = 0x08;
    buff[1] = 0x9B;
    buff[2] = 0x00;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    CHECK_ERR(err);

    DEBUG_MSG("SX1261: spectral scan aborted\n");

    _meas_time_stop(4, tm, __FUNCTION__);

    /* Restart LoRa RX if it was configured and not inhibited by TX */
    if (sx1261_lora_rx_enabled && !sx1261_tx_inhibit_rx) {
        printf("SX1261: Restarting LoRa RX after spectral scan abort\n");
        sx1261_lora_rx_restart_light();
    } else if (sx1261_lora_rx_enabled && sx1261_tx_inhibit_rx) {
        /* TX inhibit active — defer RX restart until inhibit clears */
        sx1261_deferred_rx_restart = true;
    }

    return LGW_REG_SUCCESS;
}


/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

/**
 * @brief Perform a single hardware LoRa CAD (Channel Activity Detection) scan
 *        on the SX1261 radio.
 *
 * Temporarily reconfigures the SX1261 from FSK/spectral-scan mode to LoRa mode,
 * performs a CAD check at the specified frequency/SF/BW, reads the result,
 * then returns the SX1261 to standby.
 *
 * The caller is responsible for holding mx_concent during the entire call.
 *
 * @param freq_hz         Channel center frequency in Hz
 * @param sf              Spreading factor (7-12)
 * @param bw              Bandwidth: 0=125kHz, 1=250kHz, 2=500kHz
 * @param result          Pointer to result struct (filled on return)
 * @param skip_noisefloor When true, skip Phase 2 (FSK RX noisefloor measurement).
 *                        Phase 1 and Phase 3 always run.
 *                        result->rssi_dbm will be left at -128 (no measurement).
 * @return LGW_REG_SUCCESS on success, LGW_REG_ERROR on failure
 */
int sx1261_cad_scan(uint32_t freq_hz, uint8_t sf, uint8_t bw, sx1261_cad_result_t *result, bool skip_noisefloor) {
    int err;
    uint8_t buff[16];
    int32_t freq_reg;
    uint8_t bw_reg;
    uint8_t cad_det_peak;
    uint16_t irq_status;
    int timeout_cnt = 0;
    struct timeval tm, t0, t1;
    double ms_abort, ms_fsk_rssi, ms_setup, ms_cad, ms_cleanup, ms_total;

    /* Check input parameters */
    if (result == NULL) {
        printf("ERROR: %s: result pointer is NULL\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }
    if (sf < 7 || sf > 12) {
        printf("ERROR: %s: invalid SF %u (must be 7-12)\n", __FUNCTION__, sf);
        return LGW_REG_ERROR;
    }

    /* Initialize result */
    result->detected = false;
    result->rssi_dbm = -128;
    result->status = 2; /* error until proven otherwise */
    ms_abort = ms_fsk_rssi = ms_setup = ms_cad = ms_cleanup = 0.0;

    _meas_time_start(&tm);

    /* Stop LoRa RX if active */
    if (sx1261_lora_rx_enabled) {
        sx1261_lora_rx_stop();
    }

    /* Map BW parameter to SX126x register value */
    switch (bw) {
        case 0: bw_reg = 0x04; break;  /* 125 kHz */
        case 1: bw_reg = 0x05; break;  /* 250 kHz */
        case 2: bw_reg = 0x06; break;  /* 500 kHz */
        case 3: bw_reg = 0x03; break;  /* 62.5 kHz */
        default:
            printf("ERROR: %s: invalid BW %u\n", __FUNCTION__, bw);
            return LGW_REG_ERROR;
    }

    /* CAD detection peak based on SF (SX126x datasheet) */
    switch (sf) {
        case 7:  cad_det_peak = 22; break;
        case 8:  cad_det_peak = 22; break;
        case 9:  cad_det_peak = 23; break;
        case 10: cad_det_peak = 24; break;
        case 11: cad_det_peak = 25; break;
        case 12: cad_det_peak = 26; break;
        default: cad_det_peak = 22; break;
    }

    /* ============ Phase 1: Abort spectral scan + standby ============ */
    gettimeofday(&t0, NULL);

    err = sx1261_spectral_scan_abort();
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to abort spectral scan\n", __FUNCTION__);
        result->status = 2;
        goto cad_done;
    }

    wait_ms(2); /* CRITICAL: race condition fix between abort and standby */

    buff[0] = SX1261_STDBY_RC;
    err = sx1261_reg_w(SX1261_SET_STANDBY, buff, 1);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set standby\n", __FUNCTION__);
        result->status = 2;
        goto cad_done;
    }

    gettimeofday(&t1, NULL);
    ms_abort = (t1.tv_sec - t0.tv_sec) * 1000.0 + (t1.tv_usec - t0.tv_usec) / 1000.0;

    /* ============ Phase 2: Pre-TX noise floor measurement via FSK RX ============ */
    /* When skip_noisefloor is true, skip this ~15ms measurement entirely.
     * result->rssi_dbm stays at the init value (-128) so the caller can detect
     * that no measurement was taken and use the value from the first scan. */
    if (!skip_noisefloor) {
        /* The SX1261 GetRssiInst command only returns valid RSSI when the radio
         * is actively receiving.  In STDBY_RC (post-CAD) it always reads -128.
         * Solution: briefly enter FSK continuous-RX on the target frequency,
         * read the instantaneous RSSI, then return to standby before
         * configuring LoRa for the actual CAD scan.
         * This mirrors the FSK-RX pattern used by sx1261_set_rx_params(). */
        gettimeofday(&t0, NULL);
        {
            int16_t _tx_noisefloor = -128;
            uint8_t fsk_bw_reg;

            /* Map CAD BW to FSK RX bandwidth (~2x LoRa BW for full coverage) */
            switch (bw) {
                case 3:  fsk_bw_reg = 0x0B; break; /* 62.5 kHz LoRa -> 117.3 kHz FSK */
                case 0:  fsk_bw_reg = 0x0A; break; /* 125 kHz LoRa  -> 234.3 kHz FSK */
                case 1:  fsk_bw_reg = 0x09; break; /* 250 kHz LoRa  -> 467.0 kHz FSK */
                case 2:  fsk_bw_reg = 0x09; break; /* 500 kHz LoRa  -> 467.0 kHz FSK */
                default: fsk_bw_reg = 0x0A; break;
            }

            /* 1. Set frequency synthesis mode */
            err = sx1261_reg_w(SX1261_SET_FS, buff, 0);
            if (err != LGW_REG_SUCCESS) {
                printf("WARNING: [CAD] TX noisefloor FSK: SetFS failed\n");
                goto skip_fsk_rssi;
            }

            /* 2. Set RF frequency (same as the CAD target) */
            freq_reg = SX1261_FREQ_TO_REG(freq_hz);
            buff[0] = (uint8_t)(freq_reg >> 24);
            buff[1] = (uint8_t)(freq_reg >> 16);
            buff[2] = (uint8_t)(freq_reg >> 8);
            buff[3] = (uint8_t)(freq_reg >> 0);
            err = sx1261_reg_w(SX1261_SET_RF_FREQUENCY, buff, 4);
            if (err != LGW_REG_SUCCESS) {
                printf("WARNING: [CAD] TX noisefloor FSK: SetRfFrequency failed\n");
                goto skip_fsk_rssi;
            }

            /* 3. Configure RSSI averaging window (same as LBT) */
            buff[0] = 0x08;
            buff[1] = 0x9B;
            buff[2] = 0x05 << 2;
            err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
            if (err != LGW_REG_SUCCESS) {
                printf("WARNING: [CAD] TX noisefloor FSK: WriteRegister (RSSI avg) failed\n");
                goto skip_fsk_rssi;
            }

            /* 4. Set packet type to FSK */
            buff[0] = 0x00; /* PACKET_TYPE_GFSK */
            err = sx1261_reg_w(SX1261_SET_PACKET_TYPE, buff, 1);
            if (err != LGW_REG_SUCCESS) {
                printf("WARNING: [CAD] TX noisefloor FSK: SetPacketType failed\n");
                goto skip_fsk_rssi;
            }

            /* 5. Set FSK modulation parameters (same as LBT) */
            buff[0] = 0x00;        /* BR MSB */
            buff[1] = 0x14;        /* BR */
            buff[2] = 0x00;        /* BR LSB */
            buff[3] = 0x00;        /* Gaussian BT disabled */
            buff[4] = fsk_bw_reg;  /* RX bandwidth */
            buff[5] = 0x02;        /* FDEV MSB */
            buff[6] = 0xE9;        /* FDEV */
            buff[7] = 0x0F;        /* FDEV LSB */
            err = sx1261_reg_w(SX1261_SET_MODULATION_PARAMS, buff, 8);
            if (err != LGW_REG_SUCCESS) {
                printf("WARNING: [CAD] TX noisefloor FSK: SetModulationParams failed\n");
                goto skip_fsk_rssi;
            }

            /* 6. Set FSK packet parameters (same as LBT) */
            buff[0] = 0x00;  /* Preamble length MSB */
            buff[1] = 0x20;  /* Preamble length LSB: 32 bits */
            buff[2] = 0x05;  /* Preamble detector length: 16 bits */
            buff[3] = 0x20;  /* SyncWord length: 32 bits */
            buff[4] = 0x00;  /* AddrComp: disabled */
            buff[5] = 0x01;  /* PacketType: variable size */
            buff[6] = 0xFF;  /* PayloadLength: 255 bytes */
            buff[7] = 0x00;  /* CRCType: 1 byte */
            buff[8] = 0x00;  /* Whitening: disabled */
            err = sx1261_reg_w(SX1261_SET_PACKET_PARAMS, buff, 9);
            if (err != LGW_REG_SUCCESS) {
                printf("WARNING: [CAD] TX noisefloor FSK: SetPacketParams failed\n");
                goto skip_fsk_rssi;
            }

            /* 7. Enter continuous RX mode */
            buff[0] = 0xFF;
            buff[1] = 0xFF;
            buff[2] = 0xFF;
            err = sx1261_reg_w(SX1261_SET_RX, buff, 3);
            if (err != LGW_REG_SUCCESS) {
                printf("WARNING: [CAD] TX noisefloor FSK: SetRx failed\n");
                goto skip_fsk_rssi;
            }

            /* 8. Wait for RSSI to settle (1ms is sufficient per datasheet) */
            wait_ms(1);

            /* 9. Read instantaneous RSSI */
            err = sx1261_get_rssi_inst(&_tx_noisefloor);
            if (err == LGW_REG_SUCCESS && _tx_noisefloor > -128) {
                result->rssi_dbm = _tx_noisefloor;
                printf("INFO: [CAD] TX noisefloor: %d dBm (FSK-RX)\n", _tx_noisefloor);
            } else {
                printf("WARNING: [CAD] TX noisefloor FSK RSSI read returned %d dBm (err=%d)\n",
                       _tx_noisefloor, err);
            }

    skip_fsk_rssi:
            /* 10. Return to standby before LoRa CAD configuration */
            buff[0] = SX1261_STDBY_RC;
            sx1261_reg_w(SX1261_SET_STANDBY, buff, 1);
        }

        gettimeofday(&t1, NULL);
        ms_fsk_rssi = (t1.tv_sec - t0.tv_sec) * 1000.0 + (t1.tv_usec - t0.tv_usec) / 1000.0;
    } else {
        /* Noisefloor skipped (skip_noisefloor=true) — result->rssi_dbm remains at -128 */
        ms_fsk_rssi = 0.0;
    }

    /* ============ Phase 3: Configure LoRa registers for CAD ============ */
    gettimeofday(&t0, NULL);

    /* Step 1: Set LoRa packet type */
    buff[0] = 0x01; /* PACKET_TYPE_LORA */
    err = sx1261_reg_w(SX1261_SET_PACKET_TYPE, buff, 1);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set packet type\n", __FUNCTION__);
        result->status = 2;
        goto cad_done;
    }

    /* Step 2: Set RF frequency */
    freq_reg = SX1261_FREQ_TO_REG(freq_hz);
    buff[0] = (uint8_t)(freq_reg >> 24);
    buff[1] = (uint8_t)(freq_reg >> 16);
    buff[2] = (uint8_t)(freq_reg >> 8);
    buff[3] = (uint8_t)(freq_reg >> 0);
    err = sx1261_reg_w(SX1261_SET_RF_FREQUENCY, buff, 4);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set frequency\n", __FUNCTION__);
        result->status = 2;
        goto cad_done;
    }

    /* Step 3: Set modulation parameters */
    buff[0] = sf;      /* SF */
    buff[1] = bw_reg;  /* BW */
    buff[2] = 0x01;    /* CR 4/5 */
    buff[3] = 0x00;    /* Low data rate optimize off */
    err = sx1261_reg_w(SX1261_SET_MODULATION_PARAMS, buff, 4);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set modulation params\n", __FUNCTION__);
        result->status = 2;
        goto cad_done;
    }

    /* Step 4: Set CAD parameters */
    buff[0] = 0x02;            /* cadSymbolNum: 2 symbols */
    buff[1] = cad_det_peak;    /* cadDetPeak */
    buff[2] = 10;              /* cadDetMin */
    buff[3] = 0x00;            /* cadExitMode: STDBY after CAD */
    buff[4] = 0x00;            /* cadTimeout (MSB) */
    buff[5] = 0x00;            /* cadTimeout */
    buff[6] = 0x00;            /* cadTimeout (LSB) */
    err = sx1261_reg_w(SX1261_SET_CAD_PARAMS, buff, 7);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set CAD params\n", __FUNCTION__);
        result->status = 2;
        goto cad_done;
    }

    /* Step 5: Clear IRQ status (always — need clean IRQ state) */
    buff[0] = (SX1261_IRQ_ALL >> 8) & 0xFF;
    buff[1] = (SX1261_IRQ_ALL >> 0) & 0xFF;
    err = sx1261_reg_w(SX1261_CLR_IRQ_STATUS, buff, 2);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to clear IRQ\n", __FUNCTION__);
        result->status = 2;
        goto cad_done;
    }

    /* Step 6: Set DIO IRQ params (always — need IRQ configured) */
    buff[0] = ((SX1261_IRQ_CAD_DONE | SX1261_IRQ_CAD_DETECTED) >> 8) & 0xFF;
    buff[1] = ((SX1261_IRQ_CAD_DONE | SX1261_IRQ_CAD_DETECTED) >> 0) & 0xFF;
    buff[2] = 0x00; buff[3] = 0x00;
    buff[4] = 0x00; buff[5] = 0x00;
    buff[6] = 0x00; buff[7] = 0x00;
    err = sx1261_reg_w(SX1261_SET_DIO_IRQ_PARAMS, buff, 8);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set DIO IRQ params\n", __FUNCTION__);
        result->status = 2;
        goto cad_done;
    }

    gettimeofday(&t1, NULL);
    ms_setup = (t1.tv_sec - t0.tv_sec) * 1000.0 + (t1.tv_usec - t0.tv_usec) / 1000.0;

    /* ============ Phase 4: Execute CAD scan ============ */
    gettimeofday(&t0, NULL);

    buff[0] = 0x00; /* dummy byte */
    err = sx1261_reg_w(SX1261_SET_CAD, buff, 0);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to start CAD\n", __FUNCTION__);
        result->status = 2;
        goto cad_done;
    }

    /* Poll IRQ status until CadDone (with timeout) */
    timeout_cnt = 0;
    while (timeout_cnt < 100) { /* max 100ms timeout */
        buff[0] = 0x00;
        buff[1] = 0x00;
        buff[2] = 0x00;
        err = sx1261_reg_r(SX1261_GET_IRQ_STATUS, buff, 3);
        if (err != LGW_REG_SUCCESS) {
            printf("ERROR: %s: failed to get IRQ status\n", __FUNCTION__);
            result->status = 2;
            goto cad_done;
        }

        irq_status = ((uint16_t)buff[1] << 8) | (uint16_t)buff[2];

        if (irq_status & SX1261_IRQ_CAD_DONE) {
            result->detected = (irq_status & SX1261_IRQ_CAD_DETECTED) ? true : false;
            result->status = 0; /* success */

            /* Clear IRQ flags */
            buff[0] = (SX1261_IRQ_ALL >> 8) & 0xFF;
            buff[1] = (SX1261_IRQ_ALL >> 0) & 0xFF;
            sx1261_reg_w(SX1261_CLR_IRQ_STATUS, buff, 2);
            break;
        }

        wait_ms(1);
        timeout_cnt++;
    }

    if (timeout_cnt >= 100) {
        printf("WARNING: %s: CAD timeout after %dms on freq=%uHz SF%u\n",
               __FUNCTION__, timeout_cnt, freq_hz, sf);
        result->status = 1; /* timeout */
    }

    /* NOTE: Old post-CAD RSSI read removed — it always returned -128/-127
     * because GetRssiInst requires active RX mode.  The pre-TX FSK-RX
     * measurement (Phase 2) now provides a reliable TX noise floor reading. */


    gettimeofday(&t1, NULL);
    ms_cad = (t1.tv_sec - t0.tv_sec) * 1000.0 + (t1.tv_usec - t0.tv_usec) / 1000.0;

cad_done:
    /* ============ Phase 5: GPIO reset + full reinit (with FAST bulk PRAM load) ============
     * After CAD, the SX1261 LoRa mode corrupts its internal state and PRAM.
     * A GPIO reset clears the hardware completely. The PRAM firmware patch is
     * then reloaded using an optimized bulk SPI write (~5ms vs ~430ms).
     * The JIT thread's lgw_abort_tx() handles SX1302 TX FSM recovery. */
    gettimeofday(&t0, NULL);

    /* Step 1: GPIO reset SX1261 via sysfs (path configured via lgw_set_gpio_paths()
     * from global_conf.json `gpio_paths.sx1261_reset`, defaults to SenseCAP M1
     * mapping GPIO517 = BCM5 + GPIO base offset 512). */
    {
        int fd;
        const struct lgw_gpio_paths_s *paths = lgw_get_gpio_paths();
        const char *gpio_path = paths->sx1261_reset;

        fd = open(gpio_path, O_WRONLY);
        if (fd < 0) {
            printf("ERROR: [CAD] cannot open %s for GPIO reset\n", gpio_path);
        } else {
            write(fd, "0", 1);
            wait_ms(1); /* Reset pulse 1ms (min 100us per datasheet) */
            write(fd, "1", 1);
            close(fd);
            wait_ms(3); /* Post-reset settle: 3ms (datasheet typ 2ms) */
        }
    }

    /* Step 2: Full reinit — PRAM load (fast bulk) + calibrate + setup */
    err = sx1261_load_pram();
    if (err != LGW_REG_SUCCESS) {
        printf("WARNING: [CAD] post-reset PRAM load failed\n");
    }

//     err = sx1261_calibrate(freq_hz);
//     if (err != LGW_REG_SUCCESS) {
//         printf("WARNING: [CAD] post-reset calibration failed\n");
//     }

    err = sx1261_setup();
    if (err != LGW_REG_SUCCESS) {
        printf("WARNING: [CAD] post-reset setup failed\n");
    }



    gettimeofday(&t1, NULL);
    ms_cleanup = (t1.tv_sec - t0.tv_sec) * 1000.0 + (t1.tv_usec - t0.tv_usec) / 1000.0;

    ms_total = ms_abort + ms_fsk_rssi + ms_setup + ms_cad + ms_cleanup;

    /* ============ Detailed timing log ============ */
    printf("INFO: [CAD] freq=%uHz SF%u BW%u detected=%d tx_nf=%ddBm status=%d | "
           "abort=%.1fms fsk_rssi=%.1fms setup=%.1fms cad=%.1fms(%dpolls) reinit=%.1fms TOTAL=%.1fms\n",
           freq_hz, sf, bw, result->detected, result->rssi_dbm, result->status,
           ms_abort, ms_fsk_rssi, ms_setup, ms_cad, timeout_cnt,
           ms_cleanup, ms_total);

    _meas_time_stop(4, tm, __FUNCTION__);
    return LGW_REG_SUCCESS;
}

/* -------------------------------------------------------------------------- */
/* --- SX1261 LoRa RX (Channel E) FUNCTIONS --------------------------------- */
/* -------------------------------------------------------------------------- */

/**
 * @brief Configure SX1261 for continuous LoRa RX reception (Channel E).
 *
 * Sets up the SX1261 radio for LoRa packet reception at the specified
 * frequency, bandwidth, spreading factor and coding rate. This configures
 * all necessary registers including workarounds from the datasheet.
 *
 * @param freq_hz  Center frequency in Hz
 * @param bw       Bandwidth (BW_62K5HZ=0x03, BW_125KHZ=0x04, etc)
 * @param sf       Spreading factor (7-12)
 * @param cr       Coding rate (1=4/5, 2=4/6, 3=4/7, 4=4/8)
 * @return LGW_REG_SUCCESS on success, LGW_REG_ERROR on failure
 */
int sx1261_lora_rx_configure(uint32_t freq_hz, uint8_t bw, uint8_t sf, uint8_t cr, bool boosted) {
    int err;
    uint8_t buff[16];
    int32_t freq_reg;
    uint8_t bw_reg;
    uint8_t ldo;
    uint8_t iq_reg_val;

    printf("SX1261: Configuring LoRa RX - freq=%uHz BW=0x%02X SF%u CR%u\n", freq_hz, bw, sf, cr);

    /* Validate parameters */
    if (sf < 7 || sf > 12) {
        printf("ERROR: %s: invalid SF %u (must be 7-12)\n", __FUNCTION__, sf);
        return LGW_REG_ERROR;
    }
    if (cr < 1 || cr > 4) {
        printf("ERROR: %s: invalid CR %u (must be 1-4)\n", __FUNCTION__, cr);
        return LGW_REG_ERROR;
    }

    /* Map HAL BW defines to SX126x register values */
    switch (bw) {
        case BW_62K5HZ:  bw_reg = 0x03; break;
        case BW_125KHZ:  bw_reg = 0x04; break;
        case BW_250KHZ:  bw_reg = 0x05; break;
        case BW_500KHZ:  bw_reg = 0x06; break;
        default:
            printf("ERROR: %s: unsupported BW 0x%02X\n", __FUNCTION__, bw);
            return LGW_REG_ERROR;
    }

    /* Determine if Low Data Rate Optimize is needed */
    /* LDRO is needed when symbol time > 16.38ms */
    /* For BW=62.5kHz: SF>=10 needs LDRO; BW=125kHz: SF>=11; BW=250kHz: SF>=12 */
    ldo = 0;
    if (bw == BW_62K5HZ && sf >= 10) ldo = 1;
    else if (bw == BW_125KHZ && sf >= 11) ldo = 1;
    else if (bw == BW_250KHZ && sf >= 12) ldo = 1;

    /* Store config in static vars */
    sx1261_lora_rx_freq = freq_hz;
    sx1261_lora_rx_bw = bw;
    sx1261_lora_rx_sf = sf;
    sx1261_lora_rx_cr = cr;
    sx1261_lora_rx_boosted = boosted;

    /* --- Step 1: Go to Standby --- */
    buff[0] = (uint8_t)SX1261_STDBY_RC;
    err = sx1261_reg_w(SX1261_SET_STANDBY, buff, 1);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set standby\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* --- Step 2: Set Packet Type to LoRa --- */
    buff[0] = 0x01; /* PACKET_TYPE_LORA */
    err = sx1261_reg_w(SX1261_SET_PACKET_TYPE, buff, 1);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set packet type LoRa\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* --- Step 3: Set RF Frequency --- */
    freq_reg = SX1261_FREQ_TO_REG(freq_hz);
    buff[0] = (uint8_t)(freq_reg >> 24);
    buff[1] = (uint8_t)(freq_reg >> 16);
    buff[2] = (uint8_t)(freq_reg >> 8);
    buff[3] = (uint8_t)(freq_reg >> 0);
    err = sx1261_reg_w(SX1261_SET_RF_FREQUENCY, buff, 4);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set RF frequency\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* --- Step 4: Set Buffer Base Address (TX=0, RX=0) --- */
    buff[0] = 0x00; /* TX base address */
    buff[1] = 0x00; /* RX base address */
    err = sx1261_reg_w(SX1261_SET_BUFFER_BASE_ADDRESS, buff, 2);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set buffer base address\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* --- Step 5: Set LoRa Modulation Parameters --- */
    /* SF, BW, CR, LowDataRateOptimize */
    buff[0] = sf;       /* Spreading Factor */
    buff[1] = bw_reg;   /* Bandwidth */
    buff[2] = cr;       /* CodingRate (1=CR4/5, 2=CR4/6, 3=CR4/7, 4=CR4/8) */
    buff[3] = ldo;      /* LowDataRateOptimize */
    err = sx1261_reg_w(SX1261_SET_MODULATION_PARAMS, buff, 4);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set LoRa modulation params\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* --- Step 5b: BW < 500 kHz workaround (datasheet section 15.1) --- */
    /* MUST be written AFTER SetModulationParams as that command overwrites 0x0889 */
    if (bw_reg != 0x06) { /* not 500 kHz */
        buff[0] = (SX1261_REG_BW500_WORKAROUND >> 8) & 0xFF;
        buff[1] = (SX1261_REG_BW500_WORKAROUND >> 0) & 0xFF;
        buff[2] = 0x00; /* optimization value for BW < 500 kHz */
        err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
        if (err != LGW_REG_SUCCESS) {
            printf("ERROR: %s: failed to write BW workaround register\n", __FUNCTION__);
            return LGW_REG_ERROR;
        }
        printf("SX1261: BW<500kHz workaround applied (reg 0x0889 = 0x00)\n");
    }

    /* --- Step 6: Set LoRa Packet Parameters --- */
    /* PreambleLen=16, Explicit header, MaxPayloadLen=255, CRC on, Standard IQ */
    buff[0] = 0x00; /* Preamble length MSB */
    buff[1] = 0x10; /* Preamble length LSB = 16 symbols */
    buff[2] = 0x00; /* Header type: 0x00 = explicit */
    buff[3] = 0xFF; /* Max payload length: 255 bytes */
    buff[4] = 0x01; /* CRC: 0x01 = on */
    buff[5] = 0x00; /* Invert IQ: 0x00 = standard (no inversion) */
    err = sx1261_reg_w(SX1261_SET_PACKET_PARAMS, buff, 6);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set LoRa packet params\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* --- Step 7: Set LoRa Sync Word (private network: 0x1424) --- */
    /* Register 0x0740 = MSB, 0x0741 = LSB */
    buff[0] = (SX1261_REG_LORA_SYNC_WORD_MSB >> 8) & 0xFF;
    buff[1] = (SX1261_REG_LORA_SYNC_WORD_MSB >> 0) & 0xFF;
    buff[2] = 0x14; /* Sync word MSB (private network) */
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to write sync word MSB\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }
    buff[0] = (SX1261_REG_LORA_SYNC_WORD_LSB >> 8) & 0xFF;
    buff[1] = (SX1261_REG_LORA_SYNC_WORD_LSB >> 0) & 0xFF;
    buff[2] = 0x24; /* Sync word LSB (private network) */
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to write sync word LSB\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }
    printf("SX1261: LoRa sync word set to 0x1424 (private network)\n");

    /* --- Step 8: IQ polarity fix (datasheet section 15.4) --- */
    /* Read register 0x0736, set bit 2 for standard IQ (non-inverted) */
    buff[0] = (SX1261_REG_IQ_POLARITY >> 8) & 0xFF;
    buff[1] = (SX1261_REG_IQ_POLARITY >> 0) & 0xFF;
    buff[2] = 0x00; /* dummy byte for read */
    buff[3] = 0x00; /* read value placeholder */
    err = sx1261_reg_r(SX1261_READ_REGISTER, buff, 4);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to read IQ polarity register\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }
    iq_reg_val = buff[3];
    /* For standard IQ (non-inverted): set bit 2 */
    iq_reg_val |= 0x04;
    buff[0] = (SX1261_REG_IQ_POLARITY >> 8) & 0xFF;
    buff[1] = (SX1261_REG_IQ_POLARITY >> 0) & 0xFF;
    buff[2] = iq_reg_val;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to write IQ polarity register\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }
    printf("SX1261: IQ polarity fix applied (reg 0x0736 = 0x%02X)\n", iq_reg_val);

    /* --- Step 9: Set LNA RX gain (boosted or power-saving) --- */
    /* Register 0x029F: 0x01 = boosted (max sensitivity), 0x00 = power saving */
    buff[0] = (SX1261_REG_RX_GAIN >> 8) & 0xFF;
    buff[1] = (SX1261_REG_RX_GAIN >> 0) & 0xFF;
    buff[2] = boosted ? 0x01 : 0x00;
    err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set LNA RX gain\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }
    printf("SX1261: LNA RX gain set to %s (reg 0x029F = 0x%02X)\n", boosted ? "BOOSTED" : "POWER-SAVING", buff[2]);

    /* --- Step 10: Set DIO IRQ Parameters for LoRa RX --- */
    {
        uint16_t irq_mask = SX1261_IRQ_RX_DONE | SX1261_IRQ_CRC_ERR |
                            SX1261_IRQ_HEADER_VALID | SX1261_IRQ_PREAMBLE_DETECTED;
        buff[0] = (irq_mask >> 8) & 0xFF;  /* IrqMask MSB */
        buff[1] = (irq_mask >> 0) & 0xFF;  /* IrqMask LSB */
        buff[2] = (irq_mask >> 8) & 0xFF;  /* DIO1 mask MSB */
        buff[3] = (irq_mask >> 0) & 0xFF;  /* DIO1 mask LSB */
        buff[4] = 0x00;                     /* DIO2 mask MSB */
        buff[5] = 0x00;                     /* DIO2 mask LSB */
        buff[6] = 0x00;                     /* DIO3 mask MSB */
        buff[7] = 0x00;                     /* DIO3 mask LSB */
    }
    err = sx1261_reg_w(SX1261_SET_DIO_IRQ_PARAMS, buff, 8);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set DIO IRQ params\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* Mark LoRa RX as enabled (but not yet started) */
    sx1261_lora_rx_enabled = true;

    printf("SX1261: LoRa RX configured - freq=%uHz BW=0x%02X SF%u CR%u LDRO=%u\n",
           freq_hz, bw, sf, cr, ldo);

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

/**
 * @brief Enter continuous LoRa RX mode on SX1261.
 *
 * Clears any pending IRQ flags and starts continuous RX reception.
 * Must be called after sx1261_lora_rx_configure().
 *
 * @return LGW_REG_SUCCESS on success, LGW_REG_ERROR on failure
 */
int sx1261_lora_rx_start(void) {
    int err;
    uint8_t buff[16];

    if (!sx1261_lora_rx_enabled) {
        printf("WARNING: %s: LoRa RX not configured, cannot start\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* Go to standby RC first (required for calibration) */
    buff[0] = (uint8_t)SX1261_STDBY_RC;
    err = sx1261_reg_w(SX1261_SET_STANDBY, buff, 1);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set standby\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }
    wait_ms(1);

    /* Re-enable TCXO via DIO3 (1.7V, 5ms timeout) */
    {
        uint32_t tcxo_timeout = (uint32_t)(5.0 / 0.015625);
        buff[0] = 0x01;
        buff[1] = (uint8_t)((tcxo_timeout >> 16) & 0xFF);
        buff[2] = (uint8_t)((tcxo_timeout >> 8) & 0xFF);
        buff[3] = (uint8_t)((tcxo_timeout >> 0) & 0xFF);
        sx1261_reg_w(SX1261_OP_SET_DIO3_AS_TCXO_CTRL, buff, 4);
        wait_ms(2); /* TCXO settle (was 5ms, datasheet <1ms) */
    }

    /* Full block calibration in STBY_RC */
    buff[0] = 0x7F;
    sx1261_reg_w(SX1261_CALIBRATE, buff, 1);
    wait_ms(4); /* Full calibrate (was 10ms, datasheet max 3.5ms) */

    /* Image calibration for 869 MHz */
    buff[0] = 0xD7;
    buff[1] = 0xDB;
    sx1261_reg_w(SX1261_CALIBRATE_IMAGE, buff, 2);
    wait_ms(2); /* Image calibrate (was 5ms) */

    /* Switch to STBY_XOSC */
    buff[0] = (uint8_t)SX1261_STDBY_XOSC;
    sx1261_reg_w(SX1261_SET_STANDBY, buff, 1);
    wait_ms(1);

    /* DC-DC regulator */
    buff[0] = 0x01;
    sx1261_reg_w(SX1261_SET_REGULATORMODE, buff, 1);

    /* Sensitivity adjust */
    buff[0] = 0x08;
    buff[1] = 0xAC;
    buff[2] = 0xCB;
    sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);

    /* Reconfigure modulation params (they may have been changed by LBT/CAD) */
    {
        int32_t freq_reg;
        uint8_t bw_reg, ldo;

        /* Set Packet Type to LoRa */
        buff[0] = 0x01; /* PACKET_TYPE_LORA */
        err = sx1261_reg_w(SX1261_SET_PACKET_TYPE, buff, 1);
        if (err != LGW_REG_SUCCESS) {
            printf("ERROR: %s: failed to set packet type\n", __FUNCTION__);
            return LGW_REG_ERROR;
        }

        /* Set RF Frequency */
        freq_reg = SX1261_FREQ_TO_REG(sx1261_lora_rx_freq);
        buff[0] = (uint8_t)(freq_reg >> 24);
        buff[1] = (uint8_t)(freq_reg >> 16);
        buff[2] = (uint8_t)(freq_reg >> 8);
        buff[3] = (uint8_t)(freq_reg >> 0);
        err = sx1261_reg_w(SX1261_SET_RF_FREQUENCY, buff, 4);
        if (err != LGW_REG_SUCCESS) {
            printf("ERROR: %s: failed to set frequency\n", __FUNCTION__);
            return LGW_REG_ERROR;
        }

        /* Map BW */
        switch (sx1261_lora_rx_bw) {
            case BW_62K5HZ:  bw_reg = 0x03; break;
            case BW_125KHZ:  bw_reg = 0x04; break;
            case BW_250KHZ:  bw_reg = 0x05; break;
            case BW_500KHZ:  bw_reg = 0x06; break;
            default: bw_reg = 0x03; break;
        }

        /* Determine LDRO */
        ldo = 0;
        if (sx1261_lora_rx_bw == BW_62K5HZ && sx1261_lora_rx_sf >= 10) ldo = 1;
        else if (sx1261_lora_rx_bw == BW_125KHZ && sx1261_lora_rx_sf >= 11) ldo = 1;
        else if (sx1261_lora_rx_bw == BW_250KHZ && sx1261_lora_rx_sf >= 12) ldo = 1;

        /* Set Modulation Params */
        buff[0] = sx1261_lora_rx_sf;
        buff[1] = bw_reg;
        buff[2] = sx1261_lora_rx_cr;
        buff[3] = ldo;
        err = sx1261_reg_w(SX1261_SET_MODULATION_PARAMS, buff, 4);
        if (err != LGW_REG_SUCCESS) {
            printf("ERROR: %s: failed to set modulation params\n", __FUNCTION__);
            return LGW_REG_ERROR;
        }

        /* BW < 500 kHz workaround - MUST be after SetModulationParams */
        if (bw_reg != 0x06) {
            buff[0] = (SX1261_REG_BW500_WORKAROUND >> 8) & 0xFF;
            buff[1] = (SX1261_REG_BW500_WORKAROUND >> 0) & 0xFF;
            buff[2] = 0x00;
            err = sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
            if (err != LGW_REG_SUCCESS) {
                printf("ERROR: %s: failed to write BW workaround\n", __FUNCTION__);
                return LGW_REG_ERROR;
            }
        }

        /* Set Packet Params */
        buff[0] = 0x00; /* Preamble length MSB */
        buff[1] = 0x10; /* Preamble length LSB = 16 */
        buff[2] = 0x00; /* Explicit header */
        buff[3] = 0xFF; /* Max payload 255 */
        buff[4] = 0x01; /* CRC on */
        buff[5] = 0x00; /* Standard IQ */
        err = sx1261_reg_w(SX1261_SET_PACKET_PARAMS, buff, 6);
        if (err != LGW_REG_SUCCESS) {
            printf("ERROR: %s: failed to set packet params\n", __FUNCTION__);
            return LGW_REG_ERROR;
        }

        /* Sync word */
        buff[0] = (SX1261_REG_LORA_SYNC_WORD_MSB >> 8) & 0xFF;
        buff[1] = (SX1261_REG_LORA_SYNC_WORD_MSB >> 0) & 0xFF;
        buff[2] = 0x14;
        sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);
        buff[0] = (SX1261_REG_LORA_SYNC_WORD_LSB >> 8) & 0xFF;
        buff[1] = (SX1261_REG_LORA_SYNC_WORD_LSB >> 0) & 0xFF;
        buff[2] = 0x24;
        sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);

        /* IQ polarity fix */
        buff[0] = (SX1261_REG_IQ_POLARITY >> 8) & 0xFF;
        buff[1] = (SX1261_REG_IQ_POLARITY >> 0) & 0xFF;
        buff[2] = 0x00;
        buff[3] = 0x00;
        sx1261_reg_r(SX1261_READ_REGISTER, buff, 4);
        buff[0] = (SX1261_REG_IQ_POLARITY >> 8) & 0xFF;
        buff[1] = (SX1261_REG_IQ_POLARITY >> 0) & 0xFF;
        buff[2] = buff[3] | 0x04; /* set bit 2 for standard IQ */
        sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);

        /* LNA RX gain (boosted or power-saving, from initial configure) */
        buff[0] = (SX1261_REG_RX_GAIN >> 8) & 0xFF;
        buff[1] = (SX1261_REG_RX_GAIN >> 0) & 0xFF;
        buff[2] = sx1261_lora_rx_boosted ? 0x01 : 0x00;
        sx1261_reg_w(SX1261_WRITE_REGISTER, buff, 3);

        /* Set IRQ mask */
        {
            uint16_t irq_mask = SX1261_IRQ_RX_DONE | SX1261_IRQ_CRC_ERR |
                                SX1261_IRQ_HEADER_VALID | SX1261_IRQ_PREAMBLE_DETECTED;
            buff[0] = (irq_mask >> 8) & 0xFF;
            buff[1] = (irq_mask >> 0) & 0xFF;
            buff[2] = (irq_mask >> 8) & 0xFF;
            buff[3] = (irq_mask >> 0) & 0xFF;
            buff[4] = 0x00;
            buff[5] = 0x00;
            buff[6] = 0x00;
            buff[7] = 0x00;
        }
        sx1261_reg_w(SX1261_SET_DIO_IRQ_PARAMS, buff, 8);

        /* Set RX buffer base address */
        buff[0] = 0x00;
        buff[1] = 0x00;
        sx1261_reg_w(SX1261_SET_BUFFER_BASE_ADDRESS, buff, 2);
    }

    /* Clear all pending IRQ flags */
    buff[0] = (SX1261_IRQ_ALL >> 8) & 0xFF;
    buff[1] = (SX1261_IRQ_ALL >> 0) & 0xFF;
    err = sx1261_reg_w(SX1261_CLR_IRQ_STATUS, buff, 2);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to clear IRQ status\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* Enter continuous RX mode (timeout = 0xFFFFFF) */
    buff[0] = 0xFF;
    buff[1] = 0xFF;
    buff[2] = 0xFF;
    err = sx1261_reg_w(SX1261_SET_RX, buff, 3);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to enter RX mode\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    printf("SX1261: LoRa RX started - continuous mode on %uHz\n", sx1261_lora_rx_freq);

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

/**
 * @brief Stop LoRa RX and go to standby.
 *
 * Does NOT clear the sx1261_lora_rx_enabled flag - the config is preserved
 * so LoRa RX can be restarted after LBT/CAD/spectral scan operations.
 *
 * @return LGW_REG_SUCCESS on success, LGW_REG_ERROR on failure
 */

/* ========================================================================== */
/* TX BLANKING: Lightweight pause/resume (keeps XOSC running, config intact)  */
/* ========================================================================== */

/**
 * @brief Pause SX1261 LoRa RX for TX blanking (lightweight, <1ms).
 *
 * Uses STDBY_XOSC instead of STDBY_RC to keep the crystal oscillator running
 * and all LoRa configuration intact. Resume only needs CLR_IRQ + SetRx.
 */
int sx1261_lora_rx_pause(void) {
    int err;
    uint8_t buff[2];

    if (!sx1261_lora_rx_enabled) {
        return LGW_REG_SUCCESS; /* nothing to pause */
    }

    /* Go to STDBY_XOSC - crystal keeps running, config preserved */
    buff[0] = (uint8_t)SX1261_STDBY_XOSC;
    err = sx1261_reg_w(SX1261_SET_STANDBY, buff, 1);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set STDBY_XOSC\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    DEBUG_MSG("SX1261: LoRa RX paused (STDBY_XOSC) for TX blanking\n");
    return LGW_REG_SUCCESS;
}

/**
 * @brief Resume SX1261 LoRa RX after TX blanking (lightweight, <1ms).
 *
 * Since we used STDBY_XOSC (not STDBY_RC), the crystal is still running
 * and all LoRa parameters are intact. Just clear IRQs and re-enter RX.
 */
int sx1261_lora_rx_resume(void) {
    int err;
    uint8_t buff[4];

    if (!sx1261_lora_rx_enabled) {
        return LGW_REG_SUCCESS; /* nothing to resume */
    }

    /* Clear all pending IRQ flags (from TX leakage etc.) */
    buff[0] = (SX1261_IRQ_ALL >> 8) & 0xFF;
    buff[1] = (SX1261_IRQ_ALL >> 0) & 0xFF;
    err = sx1261_reg_w(SX1261_CLR_IRQ_STATUS, buff, 2);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to clear IRQ\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* Re-enter continuous RX mode (timeout = 0xFFFFFF) */
    buff[0] = 0xFF;
    buff[1] = 0xFF;
    buff[2] = 0xFF;
    err = sx1261_reg_w(SX1261_SET_RX, buff, 3);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to resume RX\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    DEBUG_MSG("SX1261: LoRa RX resumed after TX blanking\n");
    return LGW_REG_SUCCESS;
}

int sx1261_lora_rx_stop(void) {
    int err;
    uint8_t buff[4];

    /* Go to standby */
    buff[0] = (uint8_t)SX1261_STDBY_RC;
    err = sx1261_reg_w(SX1261_SET_STANDBY, buff, 1);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to set standby\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    DEBUG_MSG("SX1261: LoRa RX stopped (standby)\n");

    return LGW_REG_SUCCESS;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

/**
 * @brief Check for and read received LoRa packets from SX1261.
 *
 * Polls the SX1261 IRQ status. If RxDone is asserted, reads the packet
 * from the radio buffer and fills in the lgw_pkt_rx_s structure.
 *
 * @param pkt_data  Pointer to array of packet structures to fill
 * @param max_pkt   Maximum number of packets to read
 * @return Number of packets found (0 or 1), or -1 on error
 */
int sx1261_lora_rx_fetch(struct lgw_pkt_rx_s *pkt_data, uint8_t max_pkt) {
    int err;
    uint8_t buff[260]; /* max payload 255 + header bytes */
    uint16_t irq_status;
    uint8_t payload_len;
    uint8_t rx_start_buf_ptr;
    int8_t snr_pkt;
    uint8_t rssi_pkt;
    uint32_t timestamp;
    static uint32_t fetch_count = 0;
    static uint32_t last_nonzero_irq = 0;

    if (pkt_data == NULL || max_pkt == 0) {
        return 0;
    }

    fetch_count++;

    /* Read IRQ status */
    buff[0] = 0x00;
    buff[1] = 0x00;
    buff[2] = 0x00;
    err = sx1261_reg_r(SX1261_GET_IRQ_STATUS, buff, 3);
    if (err != LGW_REG_SUCCESS) {
        if (fetch_count % 100 == 0) printf("SX1261-DBG: fetch #%u SPI read error\n", fetch_count);
        return -1;
    }
    irq_status = ((uint16_t)buff[1] << 8) | (uint16_t)buff[2];

    /* Debug: log every 100th call + any non-zero IRQ */
    if (fetch_count % 100 == 0) {
        /* Also read chip mode */
        uint8_t stat_buf[2] = {0, 0};
        sx1261_reg_r(SX1261_GET_STATUS, stat_buf, 1);
        uint8_t chip_mode = (stat_buf[0] >> 4) & 0x07;
        printf("SX1261-DBG: fetch #%u irq=0x%04X mode=%u last_nonzero=#%u\n",
               fetch_count, irq_status, chip_mode, last_nonzero_irq);
    }
    if (irq_status != 0) {
        last_nonzero_irq = fetch_count;
        printf("SX1261-DBG: fetch #%u IRQ=0x%04X (preamble=%d sync=%d hdr=%d rxdone=%d crc=%d)\n",
               fetch_count, irq_status,
               (irq_status >> 2) & 1, (irq_status >> 1) & 1,
               (irq_status >> 4) & 1, (irq_status >> 1) & 1,
               (irq_status >> 6) & 1);
    }

    /* Check if RxDone is asserted */
    if (!(irq_status & SX1261_IRQ_RX_DONE)) {
        return 0; /* No packet received */
    }

    /* Get current timestamp from SX1302 counter */
    err = lgw_get_instcnt(&timestamp);
    if (err != LGW_HAL_SUCCESS) {
        timestamp = 0; /* fallback if timestamp unavailable */
    }

    /* Get RX buffer status: payload length and buffer pointer */
    buff[0] = 0x00; /* status */
    buff[1] = 0x00; /* payloadLengthRx */
    buff[2] = 0x00; /* rxStartBufferPointer */
    err = sx1261_reg_r(SX1261_GET_RX_BUFFER_STATUS, buff, 3);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to get RX buffer status\n", __FUNCTION__);
        goto cleanup;
    }
    payload_len = buff[1];
    rx_start_buf_ptr = buff[2];

    if (payload_len == 0 || payload_len > 255) {
        printf("WARNING: %s: invalid payload length %u, discarding\n", __FUNCTION__, payload_len);
        goto cleanup;
    }

    /* Read payload from buffer */
    /* ReadBuffer command: offset byte + status byte + payload */
    buff[0] = rx_start_buf_ptr; /* offset */
    buff[1] = 0x00;             /* status (NOP) */
    memset(&buff[2], 0, payload_len);
    err = sx1261_reg_r(SX1261_READ_BUFFER, buff, 2 + payload_len);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to read RX buffer\n", __FUNCTION__);
        goto cleanup;
    }

    /* Get packet status: RSSI and SNR */
    {
        uint8_t pkt_status[4] = {0};
        /* GetPacketStatus returns: status, rssiPkt, snrPkt, signalRssiPkt */
        err = sx1261_reg_r(SX1261_GET_PACKET_STATUS, pkt_status, 4);
        if (err != LGW_REG_SUCCESS) {
            printf("ERROR: %s: failed to get packet status\n", __FUNCTION__);
            goto cleanup;
        }
        rssi_pkt = pkt_status[1];    /* RssiPkt: -rssi/2 dBm */
        snr_pkt = (int8_t)pkt_status[2]; /* SnrPkt: snr/4 dB (signed) */
    }

    /* Fill in the packet structure */
    memset(&pkt_data[0], 0, sizeof(struct lgw_pkt_rx_s));
    pkt_data[0].freq_hz = sx1261_lora_rx_freq;
    pkt_data[0].if_chain = 9;       /* Special marker for Channel E (SX1261 LoRa RX) */
    pkt_data[0].rf_chain = 1;       /* SX1261 is on radio_1 path */
    pkt_data[0].modulation = MOD_LORA;
    pkt_data[0].bandwidth = sx1261_lora_rx_bw;
    pkt_data[0].datarate = sx1261_lora_rx_sf;
    pkt_data[0].coderate = sx1261_lora_rx_cr;
    pkt_data[0].rssic = -(int16_t)rssi_pkt / 2;
    pkt_data[0].rssis = pkt_data[0].rssic; /* same for SX1261 */
    pkt_data[0].snr = (float)snr_pkt / 4.0;
    pkt_data[0].snr_min = pkt_data[0].snr;
    pkt_data[0].snr_max = pkt_data[0].snr;
    pkt_data[0].count_us = timestamp;
    pkt_data[0].size = payload_len;
    pkt_data[0].crc = 0; /* CRC value not directly available from SX1261 */

    /* Determine CRC status from IRQ flags */
    if (irq_status & SX1261_IRQ_CRC_ERR) {
        pkt_data[0].status = STAT_CRC_BAD;
    } else {
        pkt_data[0].status = STAT_CRC_OK;
    }

    /* Copy payload data (skip offset and status bytes) */
    memcpy(pkt_data[0].payload, &buff[2], payload_len);

    printf("SX1261: LoRa RX packet received - %u bytes, RSSI=%.1f dBm, SNR=%.1f dB, CRC=%s\n",
           payload_len, (double)pkt_data[0].rssic, (double)pkt_data[0].snr,
           (pkt_data[0].status == STAT_CRC_OK) ? "OK" : "BAD");

cleanup:
    /* Clear all IRQ flags */
    buff[0] = (SX1261_IRQ_ALL >> 8) & 0xFF;
    buff[1] = (SX1261_IRQ_ALL >> 0) & 0xFF;
    sx1261_reg_w(SX1261_CLR_IRQ_STATUS, buff, 2);

    /* Return 1 if we got a valid packet, 0 otherwise */
    if (err != LGW_REG_SUCCESS) {
        return 0;
    }
    return 1;
}

/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

/**
 * @brief Check if LoRa RX is currently enabled/configured.
 *
 * @return true if LoRa RX has been configured and is active
 */
bool sx1261_lora_rx_active(void) {
    return sx1261_lora_rx_enabled;
}


/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

/**
 * @brief Read instantaneous RSSI from SX1261.
 *
 * The SX1261 must be in RX mode (GFSK or LoRa) for this to return a valid value.
 * Uses the GetRssiInst command (0x15) which returns the current RSSI in -dBm/2 format.
 *
 * @param rssi_dbm  pointer to store the RSSI value in dBm (negative integer)
 * @return LGW_REG_SUCCESS on success, LGW_REG_ERROR on failure
 */
int sx1261_get_rssi_inst(int16_t *rssi_dbm) {
    int err;
    uint8_t buff[2];

    if (rssi_dbm == NULL) {
        printf("ERROR: %s: NULL pointer\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* GetRssiInst command: opcode 0x15, returns 1 status byte + 1 RSSI byte */
    buff[0] = 0x00; /* dummy byte for status */
    buff[1] = 0x00; /* RSSI value holder */
    err = sx1261_reg_r(SX1261_GET_RSSI_INST, buff, 2);
    if (err != LGW_REG_SUCCESS) {
        printf("ERROR: %s: failed to read RSSI\n", __FUNCTION__);
        return LGW_REG_ERROR;
    }

    /* RSSI in dBm = -RssiInst/2 */
    *rssi_dbm = -(int16_t)buff[1] / 2;

    return LGW_REG_SUCCESS;
}


/* ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ */

/**
 * @brief Set the TX inhibit flag to prevent LoRa RX restart during TX.
 *
 * When set to true, sx1261_lora_rx_restart_light() and
 * sx1261_spectral_scan_abort() will NOT restart LoRa RX.
 * This keeps the SX1261 in STDBY so the FEM stays neutral
 * and allows the SX1302 PA to activate for TX.
 *
 * @param inhibit  true to inhibit, false to allow
 */
void sx1261_set_tx_inhibit_rx(bool inhibit) {
    sx1261_tx_inhibit_rx = inhibit;
    /* WM1303: when TX inhibit is released and a deferred RX restart is
       pending, restart LoRa RX immediately to minimize RX downtime. */
    if (!inhibit && sx1261_deferred_rx_restart && sx1261_lora_rx_enabled) {
        sx1261_deferred_rx_restart = false;
        printf("SX1261: Deferred LoRa RX restart after TX inhibit cleared\n");
        sx1261_lora_rx_restart_light();
    } else if (!inhibit) {
        sx1261_deferred_rx_restart = false;
    }
}

/**
 * @brief Get the current TX inhibit flag state.
 * @return true if LoRa RX restart is currently inhibited
 */
bool sx1261_get_tx_inhibit_rx(void) {
    return sx1261_tx_inhibit_rx;
}

/* --- EOF ------------------------------------------------------------------ */
