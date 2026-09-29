#!/usr/bin/env bash
set -e

PORT=${1:-8001}
MCSQUARE_DIR=${2:-"../MCsquare"}

echo "========================================================"
echo "  Virtual PSQA - Distributed Monte Carlo Worker"
python3 -c "import fastapi, uvicorn, numpy, multipart" 2>/dev/null || {
    echo "Installing minimal worker dependencies..."
    python3 -m pip install -r requirements.txt
}

echo "Starting worker node on port ${PORT} using ${MCSQUARE_DIR}..."

python3 vpsqa_worker.py --port "${PORT}" --mcsquare-dir "${MCSQUARE_DIR}" --idle-minutes 5.0 --max-cpu-pct 30.0
