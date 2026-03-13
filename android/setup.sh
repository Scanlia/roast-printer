#!/bin/bash
# ============================================
#  Termux Print Bridge — Setup & Run
# ============================================
#
# This script sets up and runs a TCP print server on an
# Android tablet that forwards ESC/POS data to a USB
# receipt printer (e.g. Epson TM-T88V).
#
# Prerequisites:
#   1. Install Termux from F-Droid (NOT Play Store — that version is outdated)
#      https://f-droid.org/en/packages/com.termux/
#   2. Install Termux:API from F-Droid
#      https://f-droid.org/en/packages/com.termux.api/
#   3. Connect the Epson printer via USB OTG cable
#
# Run this script once to install everything:
#   bash setup.sh
#
# Then start the bridge:
#   python print_bridge.py
#
# To run on boot (optional):
#   mkdir -p ~/.termux/boot
#   echo '#!/data/data/com.termux/files/usr/bin/bash' > ~/.termux/boot/print_bridge.sh
#   echo 'cd ~/print_bridge && python print_bridge.py' >> ~/.termux/boot/print_bridge.sh
#   chmod +x ~/.termux/boot/print_bridge.sh
#   # Then install Termux:Boot from F-Droid
# ============================================

set -e

echo "=== Termux Print Bridge Setup ==="

# Update packages
pkg update -y
pkg install -y python libusb termux-api

# Install Python USB library
pip install pyusb

# List USB devices
echo ""
echo "=== USB Devices ==="
termux-usb -l
echo ""
echo "If you see your printer above, grant permission with:"
echo "  termux-usb -r /dev/bus/usb/XXX/YYY"
echo ""
echo "Then run the bridge:"
echo "  python print_bridge.py"
echo ""
echo "The server IP is:"
ip -4 addr show wlan0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}' || echo "(check Settings > Wi-Fi)"
echo ""
echo "Setup complete!"
