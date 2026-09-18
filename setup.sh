#!/usr/bin/env bash
# ============================================================
#  Virtual PSQA - Linux Bundle Setup Script
#  Creates .venv_linux, installs requirements, and builds frontend
# ============================================================
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "=== Virtual PSQA Linux Setup ==="
echo

# 1. Python virtual environment
PY="$ROOT/.venv_linux/bin/python"
if [ ! -f "$PY" ]; then
    echo "[1/4] Creating Python environment (.venv_linux)..."
    if command -v uv &>/dev/null; then
        uv venv "$ROOT/.venv_linux"
    elif command -v python3 &>/dev/null; then
        python3 -m venv "$ROOT/.venv_linux"
    else
        echo "ERROR: Python 3 is not installed or not in PATH."
        exit 1
    fi
else
    echo "[1/4] Python environment already exists (.venv_linux)"
fi

echo "[2/4] Installing Python packages into .venv_linux..."
if command -v uv &>/dev/null; then
    uv pip install -r "$ROOT/requirements.txt" --python "$PY"
elif [ -f "$ROOT/.venv_linux/bin/pip" ]; then
    "$ROOT/.venv_linux/bin/pip" install --upgrade pip
    "$ROOT/.venv_linux/bin/pip" install -r "$ROOT/requirements.txt"
else
    "$PY" -m ensurepip --upgrade 2>/dev/null || true
    "$PY" -m pip install --upgrade pip
    "$PY" -m pip install -r "$ROOT/requirements.txt"
fi

echo "[3/4] Building web frontend into frontend/dist..."
if [ -f "$ROOT/frontend/package.json" ]; then
    cd "$ROOT/frontend"
    if [ ! -d "node_modules" ]; then
        if command -v npm &>/dev/null; then
            npm install
        else
            echo "ERROR: npm not found. Please install Node.js 18+ and npm."
            exit 1
        fi
    else
        echo "      node_modules present — skipping npm install"
    fi
    if command -v npm &>/dev/null; then
        npm run build
    fi
    cd "$ROOT"
fi

echo "[4/4] Preparing configuration and binary permissions..."
if [ ! -f "$ROOT/backend/.env" ]; then
    if [ -f "$ROOT/backend/.env.example" ]; then
        cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
        echo "      Created backend/.env from template."
    fi
fi

if [ -d "$ROOT/MCsquare" ]; then
    find "$ROOT/MCsquare" -maxdepth 2 -type f -name "MCsquare*" ! -name "*.exe" ! -name "*.bat" -exec chmod +x {} + 2>/dev/null || true
    find "$ROOT/MCsquare" -type f \( -name "*.txt" -o -name "*.dat" -o -name "*.mhd" \) -exec sed -i 's/\r$//' {} + 2>/dev/null || true
fi

chmod +x "$ROOT/run.sh" 2>/dev/null || true
chmod +x "$ROOT/setup.sh" 2>/dev/null || true

touch "$ROOT/.bundle_ready"

echo
echo "============================================================"
echo " Setup complete!"
echo " Start Virtual PSQA by running: ./run.sh"
echo "============================================================"
