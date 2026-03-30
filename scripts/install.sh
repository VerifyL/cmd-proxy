#!/bin/bash
# Quick install script for cmd-proxy

set -e

echo "Installing cmd-proxy..."
pip3 install .

echo "Creating config directory..."
sudo mkdir -p /etc/cmd-proxy

if [ ! -f /etc/cmd-proxy/config.yaml ]; then
    echo "Creating default config..."
    sudo tee /etc/cmd-proxy/config.yaml <<EOF
commands:
  mstpctl:
    sudo: true
    max_args: 10
    arg_patterns: '^[a-zA-Z0-9_.-]+$'
  ping:
    sudo: false
    max_args: 0
    virtual: true
EOF
fi

echo "Creating systemd service..."
sudo tee /etc/systemd/system/cmd-proxy.service <<EOF
[Unit]
Description=Command Proxy Server
After=network.target

[Service]
ExecStart=$(which cmd-proxy-server) -c /etc/cmd-proxy/config.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable cmd-proxy
sudo systemctl start cmd-proxy

echo "Service started. Check status with: sudo systemctl status cmd-proxy"