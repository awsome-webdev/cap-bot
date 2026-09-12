#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

# Ensure script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "[-] Please run as root (use sudo)."
  exit 1
fi

REPO_URL="https://github.com/awsome-webdev/cap-bot"
INSTALL_DIR="/home/cap-bot"
SERVICE_NAME="bot.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

echo "[+] Step 1: Checking and installing system dependencies (python3, pip, git)..."
apt-get update -y
apt-get install -y python3 python3-pip git curl

echo "[+] Step 2: Cloning or updating repository from ${REPO_URL}..."
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[*] Repository already exists at ${INSTALL_DIR}. Pulling latest changes..."
    cd "$INSTALL_DIR"
    git pull
else
    echo "[*] Cloning repository into ${INSTALL_DIR}..."
    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "$INSTALL_DIR"
    }
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

echo "[+] Step 3: Verifying python and pip paths..."
PYTHON_PATH=$(which python3)
PIP_PATH=$(which pip3)
echo "[*] Python: $PYTHON_PATH"
echo "[*] Pip: $PIP_PATH"

echo "[+] Step 4: Installing requirements from requirements.txt..."
if [ -f "requirements.txt" ]; then
    $PIP_PATH install --no-cache-dir -r requirements.txt
else
    echo "[!] Warning: requirements.txt not found!"
fi

echo "[+] Step 5: Running display setup script..."
if [ -f "setup_display.sh" ]; then
    chmod +x setup_display.sh
    # Run the display setup script (handles Xvfb / display environment)
    ./setup_display.sh
else
    echo "[!] Warning: setup_display.sh not found in repository root!"
fi

echo "[+] Step 6: Generating systemd service file dynamically..."
cat << EOF > "$SERVICE_PATH"
[Unit]
Description=Cap Bot Automation Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment="DISPLAY=:99"
Environment="MASTER_URL=https://cap.awdv.dev"
Environment="SECRET_KEY=botmaster-secret"
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON_PATH $INSTALL_DIR/node.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[*] Service file written successfully to ${SERVICE_PATH}"

echo "[+] Step 7: Enabling and starting systemd service..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "=========================================="
echo "[✓] Installation complete and service started!"
echo "------------------------------------------"
systemctl status "$SERVICE_NAME" --no-pager
echo "=========================================="
