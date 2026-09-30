#!/usr/bin/env bash
set -e

SERVER_URL=${1:-""}
MCSQUARE_DIR=${2:-"../MCsquare"}

echo "========================================================"
echo "  Virtual PSQA - Distributed Monte Carlo Worker"
echo "========================================================"

python3 -c "import httpx, numpy" 2>/dev/null || {
    echo "Installing worker dependencies..."
    python3 -m pip install -r requirements.txt
}

if [ -f "server_url.txt" ] && [ -z "$SERVER_URL" ]; then
    SERVER_URL=$(cat server_url.txt)
fi

if [ -z "$SERVER_URL" ]; then
    read -p "Enter Virtual PSQA Server URL [or press Enter for push mode on 8001]: " USER_INPUT
    if [ -n "$USER_INPUT" ]; then
        SERVER_URL="$USER_INPUT"
        echo "$USER_INPUT" > server_url.txt
    fi
fi

if [ -n "$SERVER_URL" ]; then
    echo "Starting worker in PULL (Outbound) Mode..."
    echo "Connecting to Server: ${SERVER_URL}"
    python3 vpsqa_worker.py --server-url "${SERVER_URL}" --mcsquare-dir "${MCSQUARE_DIR}" --idle-minutes 5.0 --max-cpu-pct 30.0
else
    echo "Starting worker in PUSH Mode on port 8001..."
    python3 vpsqa_worker.py --port 8001 --mcsquare-dir "${MCSQUARE_DIR}" --idle-minutes 5.0 --max-cpu-pct 30.0
fi
