#!/bin/sh
# Auto-generated power cycle script for WM1303 CoreCell
# Full power cycle to clear SX1250 TX-induced desensitization

SX1302_RESET_PIN=529
SX1302_POWER_EN_PIN=530
SX1261_RESET_PIN=517
AD5338R_RESET_PIN=525

for pin in ${SX1302_RESET_PIN} ${SX1261_RESET_PIN} ${SX1302_POWER_EN_PIN} ${AD5338R_RESET_PIN}; do
    echo "${pin}" > /sys/class/gpio/export 2>/dev/null || true
    sleep 0.1
    echo "out" > /sys/class/gpio/gpio${pin}/direction
    sleep 0.1
done

echo "Power OFF CoreCell..."
echo "0" > /sys/class/gpio/gpio${SX1302_POWER_EN_PIN}/value
# 10s drain: long enough for all bulk/decoupling caps to fully discharge so the
# SX1302 internal SRAM (AGC/ARB MCU memory, channelizer state) is wiped clean.
# Short drains (~3s) can leave residual charge, allowing stale state to survive
# the power cycle and prevent recovery from a stuck channel/calibration state.
sleep 10

echo "Power ON CoreCell..."
echo "1" > /sys/class/gpio/gpio${SX1302_POWER_EN_PIN}/value
sleep 0.5

echo "CoreCell reset..."
echo "1" > /sys/class/gpio/gpio${SX1302_RESET_PIN}/value; sleep 0.1
echo "0" > /sys/class/gpio/gpio${SX1302_RESET_PIN}/value; sleep 0.1

echo "SX1261 reset..."
echo "0" > /sys/class/gpio/gpio${SX1261_RESET_PIN}/value; sleep 0.1
echo "1" > /sys/class/gpio/gpio${SX1261_RESET_PIN}/value; sleep 0.1

echo "AD5338R reset..."
echo "0" > /sys/class/gpio/gpio${AD5338R_RESET_PIN}/value; sleep 0.1
echo "1" > /sys/class/gpio/gpio${AD5338R_RESET_PIN}/value; sleep 0.1

sleep 1
echo "Power cycle complete"
