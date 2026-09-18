#!/usr/bin/env bash
# ============================================================
#  Virtual PSQA - Linux Launcher (Arch Linux & generic Unix)
# ============================================================
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PSQA_PORT="${PSQA_PORT:-8000}"
export PYTHONPATH="$ROOT/backend:$PYTHONPATH"

# 1. Locate Python virtual environment
if [ -d "$ROOT/.venv_linux" ] && [ -f "$ROOT/.venv_linux/bin/python" ]; then
    PY="$ROOT/.venv_linux/bin/python"
elif [ -d "$ROOT/.venv" ] && [ -f "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
elif command -v uv &>/dev/null; then
    uv venv "$ROOT/.venv_linux"
    "$ROOT/.venv_linux/bin/pip" install -r requirements.txt
    PY="$ROOT/.venv_linux/bin/python"
elif command -v python3 &>/dev/null; then
    PY="python3"
else
    echo "ERROR: Python 3 is not installed or not in PATH."
    exit 1
fi

# 2. Check if frontend is built
if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
    echo "Frontend build not found. Building React frontend..."
    if command -v npm &>/dev/null; then
        (cd "$ROOT/frontend" && npm install && npm run build)
    else
        echo "WARNING: npm not found. Run 'npm install && npm run build' in frontend/ to serve the UI."
    fi
fi

# 3. Ensure MCsquare binary permissions & normalize config line endings
if [ -d "$ROOT/MCsquare" ]; then
    find "$ROOT/MCsquare" -maxdepth 2 -type f -name "MCsquare*" ! -name "*.exe" ! -name "*.bat" -exec chmod +x {} + 2>/dev/null || true
    find "$ROOT/MCsquare" -type f \( -name "*.txt" -o -name "*.dat" -o -name "*.mhd" \) -exec sed -i 's/\r$//' {} + 2>/dev/null || true
fi

# 4. HTTPS / SSL Configuration (HIPAA § 164.312(e))
SSL_ARGS=""
PROTOCOL="http"
if [ "${SSL_ENABLED:-true}" != "false" ]; then
    if [ ! -f "$ROOT/backend/certs/cert.pem" ] || [ ! -f "$ROOT/backend/certs/key.pem" ]; then
        if command -v openssl &>/dev/null; then
            echo "Generating local TLS/HTTPS certificates for HIPAA transmission security..."
            mkdir -p "$ROOT/backend/certs"
            openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
              -keyout "$ROOT/backend/certs/key.pem" \
              -out "$ROOT/backend/certs/cert.pem" \
              -subj "/C=US/ST=Medical/L=Clinic/O=Virtual PSQA/OU=Radiation Oncology/CN=virtual-psqa.local" \
              -addext "subjectAltName=DNS:localhost,DNS:virtual-psqa.local,IP:127.0.0.1" &>/dev/null || true
        fi
    fi
    if [ -f "$ROOT/backend/certs/cert.pem" ] && [ -f "$ROOT/backend/certs/key.pem" ]; then
        SSL_ARGS="--ssl-keyfile $ROOT/backend/certs/key.pem --ssl-certfile $ROOT/backend/certs/cert.pem"
        PROTOCOL="https"
    fi
fi

echo "============================================================"
echo " Virtual PSQA (HIPAA Secured) running at ${PROTOCOL}://localhost:${PSQA_PORT}"
echo " Local Network:    ${PROTOCOL}://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):${PSQA_PORT}"
echo " Encryption:       $([ "$PROTOCOL" = "https" ] && echo "TLS/HTTPS Active (§ 164.312(e))" || echo "Plain HTTP (Insecure)")"
echo " Session Timeout:  15-min Inactivity Auto-Logoff (§ 164.312(a)(2)(iii))"
echo " Audit Logging:    Active (§ 164.312(b))"
echo " Using Python:     $PY"
echo " Press Ctrl+C to stop."
echo "============================================================"

cd "$ROOT/backend"
exec "$PY" -m uvicorn main:app --host 0.0.0.0 --port "$PSQA_PORT" $SSL_ARGS --reload --reload-dir "$ROOT/backend" --reload-exclude "data/*" --reload-exclude "tests/*"
