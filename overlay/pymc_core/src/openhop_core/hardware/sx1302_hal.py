"""
Minimal stub for SX1302HAL - actual HAL operations are handled
by lora_pkt_fwd subprocess in WM1303Backend.
"""

LGW_RADIO_TYPE_SX1250 = 5
LGW_MULTI_SF_EN = 0xFF
LGW_BW_125KHZ = 0x04
LGW_BW_250KHZ = 0x05
LGW_BW_500KHZ = 0x06
LGW_SF_7 = 7
LGW_SF_8 = 8
LGW_SF_9 = 9
LGW_SF_10 = 10
LGW_SF_11 = 11
LGW_SF_12 = 12
LGW_CR_4_5 = 0x01
LGW_MOD_LORA = 0x10

def bw_hz_to_hal(hz):
    return {125000: LGW_BW_125KHZ, 250000: LGW_BW_250KHZ, 500000: LGW_BW_500KHZ}.get(int(hz), LGW_BW_125KHZ)

def sf_to_hal(sf):
    return int(sf)

def cr_to_hal(cr):
    m = {"4/5": LGW_CR_4_5, 5: LGW_CR_4_5, "4/6": 2, 6: 2, "4/7": 3, 7: 3, "4/8": 4, 8: 4}
    return m.get(cr, LGW_CR_4_5)


class lgw_pkt_tx_s:
    pass


class SX1302HAL:
    """Stub — real HAL is inside lora_pkt_fwd subprocess."""
    pass
