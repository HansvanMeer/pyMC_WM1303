#!/bin/sh
# detect_hardware.sh -- Hardware introspection helper for pyMC_WM1303
#
# Run this on a target board when adapting pyMC_WM1303 to a non-SenseCAP M1
# host (e.g. a different SX1302/SX1303 Pi HAT). It collects the information
# needed to set the correct GPIO pin numbers and SPI device path in
# config/reset_lgw.sh, config/power_cycle_lgw.sh, and config/global_conf.json.
#
# Safe to run as a normal user; some sections require root to inspect /sys.
# Exit status is always 0 -- this is a read-only inspection tool.

set -u

echo '=========================================================='
echo 'pyMC_WM1303 hardware detection'
echo '=========================================================='
echo 'Date     :' "$(date -Iseconds 2>/dev/null || date)"
echo 'Hostname :' "$(hostname 2>/dev/null || echo unknown)"
echo

echo '------ 1. System / kernel ------'
if [ -r /proc/device-tree/model ]; then
    printf 'Board    : '
    tr -d '\000' < /proc/device-tree/model
    echo
else
    echo 'Board    : (unknown -- no /proc/device-tree/model)'
fi
echo 'Kernel   :' "$(uname -srm 2>/dev/null || echo unknown)"
if [ -r /etc/os-release ]; then
    . /etc/os-release
    echo 'OS       :' "${PRETTY_NAME:-unknown}"
fi
echo

echo '------ 2. GPIO chip base offsets ------'
echo 'pyMC_WM1303 uses BCM pin numbers + the kernel GPIO base offset.'
echo 'SenseCAP M1 (Pi 4 Bookworm) uses offset 512. Other boards/kernels may differ.'
echo
if ls /sys/class/gpio/gpiochip*/base >/dev/null 2>&1; then
    for f in /sys/class/gpio/gpiochip*/base; do
        chip=$(basename "$(dirname "$f")")
        base=$(cat "$f" 2>/dev/null || echo '?')
        label='?'
        [ -r "$(dirname "$f")/label" ] && label=$(cat "$(dirname "$f")/label" 2>/dev/null || echo '?')
        ngpio='?'
        [ -r "$(dirname "$f")/ngpio" ] && ngpio=$(cat "$(dirname "$f")/ngpio" 2>/dev/null || echo '?')
        echo "  ${chip}: base=${base} ngpio=${ngpio} label=${label}"
    done
else
    echo '  (no /sys/class/gpio/gpiochip* entries -- legacy gpio interface not available)'
fi
if command -v gpioinfo >/dev/null 2>&1; then
    echo
    echo '  gpioinfo summary (first 5 chips):'
    gpioinfo 2>/dev/null | grep -E '^gpiochip' | head -5 | sed 's/^/    /'
else
    echo '  (gpioinfo not installed -- install gpiod for richer info: sudo apt install gpiod)'
fi
echo

echo '------ 3. SPI devices ------'
echo 'pyMC_WM1303 expects /dev/spidev0.0 for SX1302 and /dev/spidev0.1 for SX1261.'
if ls /dev/spidev* >/dev/null 2>&1; then
    ls -l /dev/spidev* 2>/dev/null | sed 's/^/  /'
else
    echo '  (none -- enable SPI: sudo raspi-config -> Interface -> SPI -> Yes, then reboot)'
fi
echo
if [ -r /boot/firmware/config.txt ]; then
    echo '  config.txt SPI lines:'
    grep -nE '^[^#]*spi' /boot/firmware/config.txt 2>/dev/null | sed 's/^/    /' || echo '    (none)'
elif [ -r /boot/config.txt ]; then
    echo '  config.txt SPI lines:'
    grep -nE '^[^#]*spi' /boot/config.txt 2>/dev/null | sed 's/^/    /' || echo '    (none)'
fi
echo

echo '------ 4. USB devices (for USB-attached gateways) ------'
if command -v lsusb >/dev/null 2>&1; then
    lsusb 2>/dev/null | grep -iE 'lora|semtech|rak|seeed|microchip|silicon|ftdi' | sed 's/^/  /' || true
    echo '  (full list):'
    lsusb 2>/dev/null | sed 's/^/    /'
else
    echo '  (lsusb not installed)'
fi
echo

echo '------ 5. Currently exported GPIO pins ------'
any=0
for d in /sys/class/gpio/gpio*; do
    [ -d "$d" ] || continue
    base=$(basename "$d")
    case "$base" in
        gpiochip*) continue ;;
    esac
    pin=${base#gpio}
    case "$pin" in
        ''|*[!0-9]*) continue ;;
    esac
    dir=$(cat "$d/direction" 2>/dev/null || echo '?')
    val=$(cat "$d/value" 2>/dev/null || echo '?')
    echo "  gpio${pin}: direction=${dir} value=${val}"
    any=1
done
[ "$any" -eq 0 ] && echo '  (no GPIO pins currently exported)'
echo

echo '------ 6. SenseCAP M1 reference (for comparison) ------'
echo 'On a SenseCAP M1 (Pi 4 Bookworm) the expected mapping is:'
echo '  SX1302_RESET_PIN    = 529   (BCM 17 + offset 512)'
echo '  SX1302_POWER_EN_PIN = 530   (BCM 18 + offset 512)'
echo '  SX1261_RESET_PIN    = 517   (BCM 5  + offset 512)'
echo '  AD5338R_RESET_PIN   = 525   (BCM 13 + offset 512)'
echo '  SPI: /dev/spidev0.0 (SX1302) and /dev/spidev0.1 (SX1261)'
echo
echo 'If your board uses different BCM pins, edit:'
echo '  config/reset_lgw.sh'
echo '  config/power_cycle_lgw.sh'
echo 'and set the four *_PIN variables to (BCM number) + (your GPIO base offset above).'
echo 'If your SPI bus is on a different device path, edit config/global_conf.json'
echo 'or pass --device to lora_pkt_fwd.'
echo
echo '=========================================================='
echo 'Detection complete. Share this output when reporting hardware'
echo 'compatibility issues on https://github.com/HansvanMeer/pyMC_WM1303/issues'
echo '=========================================================='

exit 0
