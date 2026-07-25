#!/usr/bin/env bash
# ============================================================
# build_and_flash.sh - Build & Flash firmware to Lilygo T-Encoder Pro
# Uses the global ~/.platformio toolchain if available.
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
FIRMWARE_DIR="$PROJECT_ROOT/firmware"
VENV_DIR="$PROJECT_ROOT/.venv"
PIO="$VENV_DIR/bin/pio"

# Use global ~/.platformio which already has the ESP32-S3 toolchain
# Only fall back to local .platformio if global doesn't exist
if [ -d "$HOME/.platformio/packages" ]; then
    echo "[INFO] Using global PlatformIO toolchain at ~/.platformio"
else
    export PLATFORMIO_CORE_DIR="$PROJECT_ROOT/.platformio"
    echo "[INFO] Using local PlatformIO toolchain at $PLATFORMIO_CORE_DIR"
fi

echo "============================================"
echo "  Agent Monitor Firmware Build & Flash"
echo "============================================"

# 1. Check venv and PlatformIO
if [ ! -f "$PIO" ]; then
    echo "[ERROR] PlatformIO not found at $PIO"
    echo "Run: python3 -m venv .venv && .venv/bin/pip install platformio"
    exit 1
fi

# 2. Detect serial port
PORT=$("$PIO" device list --json-output 2>/dev/null | python3 -c "
import sys, json
devices = json.load(sys.stdin)
for d in devices:
    hwid = d.get('hwid', '').upper()
    port = d.get('port', '')
    desc = d.get('description', '').lower()
    if '303A' in hwid or 'usbmodem' in port.lower() or 'esp32' in desc:
        print(port)
        break
" 2>/dev/null || true)

if [ -z "$PORT" ]; then
    echo "[WARN] Could not auto-detect Lilygo port. PlatformIO will attempt auto-detection."
else
    echo "[INFO] Detected hardware on: $PORT"
    export PLATFORMIO_UPLOAD_PORT="$PORT"
fi

# 3. Build firmware
echo ""
echo "[STEP 1/2] Building firmware..."
cd "$FIRMWARE_DIR"
"$PIO" run

# 4. Upload firmware
echo ""
echo "[STEP 2/2] Uploading firmware to device..."
"$PIO" run -t upload

echo ""
echo "============================================"
echo "  Firmware flashed successfully!"
echo "  Open serial monitor: $PIO device monitor"
echo "============================================"
