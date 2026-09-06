#!/bin/bash
# ==============================================================================
# Script: setup_display.sh
# Description: Installs all required dependencies (Xvfb, Chrome, Python libraries)
#              and starts a persistent background Xvfb virtual display (:99).
# ==============================================================================

DISPLAY_NUM=":99"
RESOLUTION="1024x768x24"

echo "=== [1/3] Checking & Installing System Dependencies ==="

NEEDS_UPDATE=0

# Check Xvfb
if ! command -v Xvfb >/dev/null 2>&1; then
    echo "[-] Xvfb is missing. Will install..."
    NEEDS_UPDATE=1
else
    echo "[+] Xvfb is installed."
fi

# Check wget
if ! command -v wget >/dev/null 2>&1; then
    echo "[-] wget is missing. Will install..."
    NEEDS_UPDATE=1
fi

# Check Chrome
if ! command -v google-chrome >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1; then
    echo "[-] Chrome is missing. Will install Google Chrome..."
    INSTALL_CHROME=1
    NEEDS_UPDATE=1
else
    echo "[+] Chrome is installed."
    INSTALL_CHROME=0
fi

# Update apt if needed
if [ "$NEEDS_UPDATE" -eq 1 ]; then
    apt-get update -y
fi

# Install APT packages
if ! command -v Xvfb >/dev/null 2>&1; then
    apt-get install -y xvfb
fi

if ! command -v wget >/dev/null 2>&1; then
    apt-get install -y wget
fi

if [ "$INSTALL_CHROME" -eq 1 ]; then
    wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -O /tmp/chrome.deb
    apt-get install -y /tmp/chrome.deb
    rm -f /tmp/chrome.deb
fi

echo "=== [2/3] Checking & Installing Python Packages ==="

python3 -c "import pynput" >/dev/null 2>&1 || pip3 install pynput
python3 -c "import undetected_chromedriver" >/dev/null 2>&1 || pip3 install undetected-chromedriver

echo "=== [3/3] Starting Virtual Display in Background ==="

# Check if Xvfb is already running on :99
if pgrep -f "Xvfb $DISPLAY_NUM" >/dev/null 2>&1; then
    echo "[+] Xvfb virtual display is already running on $DISPLAY_NUM."
else
    echo "[-] Starting Xvfb on display $DISPLAY_NUM ($RESOLUTION)..."
    Xvfb $DISPLAY_NUM -screen 0 $RESOLUTION >/dev/null 2>&1 &
    sleep 1
    echo "[+] Xvfb virtual display started in background."
fi

# Make DISPLAY persistent in ~/.bashrc if not already set
if ! grep -q "export DISPLAY=$DISPLAY_NUM" ~/.bashrc 2>/dev/null; then
    echo "export DISPLAY=$DISPLAY_NUM" >> ~/.bashrc
    echo "[+] Added 'export DISPLAY=$DISPLAY_NUM' to ~/.bashrc"
fi

# Export for current environment
export DISPLAY=$DISPLAY_NUM

echo ""
echo "=========================================================="
echo " SUCCESS: Virtual display $DISPLAY_NUM is active and ready!"
echo " Dependencies installed: Xvfb, Chrome, pynput, undetected-chromedriver"
echo ""
echo " To enable DISPLAY in your current terminal session, run:"
echo "    export DISPLAY=:99"
echo "=========================================================="
