#!/bin/bash
set -e

echo "=== Setting up Lumen-Zero on Raspberry Pi ==="

# Install system dependencies
sudo apt-get update
sudo apt-get install -y v4l-utils python3-pil python3-rpi.gpio

# Copy systemd service
SERVICE_PATH="/etc/systemd/system/lumen-pi.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sudo cp "${SCRIPT_DIR}/lumen-pi.service" "${SERVICE_PATH}"
sudo sed -i "s|/home/pi/Beckend|${SCRIPT_DIR}|g" "${SERVICE_PATH}"
sudo systemctl daemon-reload
sudo systemctl enable lumen-pi.service
sudo systemctl restart lumen-pi.service

echo "=== Lumen-Zero Service Installed and Started! ==="
sudo systemctl status lumen-pi.service --no-pager
