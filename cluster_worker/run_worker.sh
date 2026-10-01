#!/usr/bin/env bash
# ============================================================
#  Virtual PSQA - Distributed Monte Carlo Worker Launcher
# ============================================================
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "========================================================"
echo "  Virtual PSQA - Distributed Monte Carlo Worker"
echo "========================================================"

# 1. Locate Python environment (prefer local or root venv, create if needed)
PY=""
if [ -d "$ROOT/.venv" ] && [ -f "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
elif [ -d "$ROOT/../.venv" ] && [ -f "$ROOT/../.venv/bin/python" ]; then
    PY="$ROOT/../.venv/bin/python"
elif [ -d "$ROOT/../.venv_linux" ] && [ -f "$ROOT/../.venv_linux/bin/python" ]; then
    PY="$ROOT/../.venv_linux/bin/python"
elif command -v uv &>/dev/null; then
    echo "Creating Python virtual environment (.venv) using uv..."
    uv venv "$ROOT/.venv"
    uv pip install -r "$ROOT/requirements.txt" --python "$ROOT/.venv/bin/python"
    PY="$ROOT/.venv/bin/python"
elif command -v python3 &>/dev/null; then
    if [ ! -d "$ROOT/.venv" ]; then
        echo "Creating Python virtual environment (.venv)..."
        python3 -m venv "$ROOT/.venv"
    fi
    PY="$ROOT/.venv/bin/python"
else
    echo "ERROR: Python 3 is not installed or not in PATH."
    exit 1
fi

# 2. Check and install worker dependencies
if ! "$PY" -c "import httpx, numpy, fastapi, uvicorn" &>/dev/null; then
    echo "Installing worker dependencies..."
    if [ -f "$(dirname "$PY")/pip" ]; then
        "$(dirname "$PY")/pip" install -r "$ROOT/requirements.txt"
    else
        "$PY" -m pip install -r "$ROOT/requirements.txt"
    fi
fi

# 3. Locate MCsquare directory and verify execution permissions
MCSQUARE_DIR="../MCsquare"
if [ ! -d "$MCSQUARE_DIR" ] && [ -d "./MCsquare" ]; then
    MCSQUARE_DIR="./MCsquare"
fi
if [ -d "$MCSQUARE_DIR" ]; then
    find "$MCSQUARE_DIR" -maxdepth 2 -type f -name "MCsquare_linux*" -exec chmod +x {} + 2>/dev/null || true
fi

# 4. Command line arguments handling
# Usage:
#   run_worker.sh                               (uses saved settings or prompts)
#   run_worker.sh 0                             (dedicated mode: 0 idle minutes)
#   run_worker.sh 0 Worker-2                    (dedicated mode + node ID)
#   run_worker.sh https://server:8000           (sets server URL)
#   run_worker.sh https://server:8000 0         (sets server URL + 0 idle minutes)
#   run_worker.sh https://server:8000 0 Worker-2
#   run_worker.sh reset                         (clears saved URL and settings)
#   run_worker.sh push                          (runs inbound PUSH mode on 8001)

ARG1="$1"
ARG2="$2"
ARG3="$3"

if [ "$ARG1" = "reset" ] || [ "$ARG1" = "clear" ] || [ "$ARG1" = "--reset" ]; then
    rm -f "$ROOT/server_url.txt" "$ROOT/idle_minutes.txt"
    echo "[INFO] Reset saved server URL and configuration."
    ARG1=""
    ARG2=""
    ARG3=""
fi

MODE=""
SERVER_URL=""
IDLE_MINUTES=""
NODE_ID=""

if [ "$ARG1" = "push" ]; then
    MODE="push"
elif [ -n "$ARG1" ]; then
    if [[ "$ARG1" =~ :// ]] || [[ "$ARG1" =~ ^[0-9]+\.[0-9]+ ]]; then
        SERVER_URL="$ARG1"
        echo "$SERVER_URL" > "$ROOT/server_url.txt"
        if [ -n "$ARG2" ]; then
            IDLE_MINUTES="$ARG2"
            echo "$IDLE_MINUTES" > "$ROOT/idle_minutes.txt"
        fi
        if [ -n "$ARG3" ]; then
            NODE_ID="$ARG3"
        fi
    else
        IDLE_MINUTES="$ARG1"
        echo "$IDLE_MINUTES" > "$ROOT/idle_minutes.txt"
        if [ -n "$ARG2" ]; then
            NODE_ID="$ARG2"
        fi
    fi
fi

if [ -z "$SERVER_URL" ] && [ -f "$ROOT/server_url.txt" ]; then
    SERVER_URL=$(cat "$ROOT/server_url.txt" | tr -d '\r\n')
fi
if [ -z "$IDLE_MINUTES" ] && [ -f "$ROOT/idle_minutes.txt" ]; then
    IDLE_MINUTES=$(cat "$ROOT/idle_minutes.txt" | tr -d '\r\n')
fi

# If neither command line nor saved file set the URL, prompt the user
if [ -z "$SERVER_URL" ] && [ "$MODE" != "push" ]; then
    echo ""
    read -p "Server URL [e.g. 192.168.246.85:8000, or type 'push' for port 8001]: " USER_INPUT
    USER_INPUT=$(echo "$USER_INPUT" | tr -d '\r\n')
    if [ "$USER_INPUT" = "push" ]; then
        MODE="push"
    elif [ -n "$USER_INPUT" ]; then
        SERVER_URL="$USER_INPUT"
        echo "$SERVER_URL" > "$ROOT/server_url.txt"
    fi
fi

IDLE_PARAM=""
if [ -n "$IDLE_MINUTES" ]; then
    IDLE_PARAM="--idle-minutes $IDLE_MINUTES"
fi

NODE_PARAM=""
if [ -n "$NODE_ID" ]; then
    NODE_PARAM="--node-id $NODE_ID --name $NODE_ID"
fi

if [ -n "$SERVER_URL" ] && [ "$MODE" != "push" ]; then
    echo "Starting worker in PULL (Outbound) Mode..."
    echo "Connecting to Server: ${SERVER_URL}"
    echo "MCsquare Directory:   ${MCSQUARE_DIR}"
    exec "$PY" vpsqa_worker.py --server-url "${SERVER_URL}" --mcsquare-dir "${MCSQUARE_DIR}" ${IDLE_PARAM} ${NODE_PARAM}
else
    echo "Starting worker in PUSH Mode on port 8001..."
    echo "MCsquare Directory: ${MCSQUARE_DIR}"
    exec "$PY" vpsqa_worker.py --port 8001 --mcsquare-dir "${MCSQUARE_DIR}" ${IDLE_PARAM:-"--idle-minutes 5.0"} ${NODE_PARAM}
fi
