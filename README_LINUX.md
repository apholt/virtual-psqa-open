# Virtual PSQA — Linux Deployment & User Guide

Virtual Patient-Specific Quality Assurance (PSQA) platform for proton pencil beam scanning (PBS) radiotherapy. Built with **FastAPI**, **React / Vite**, and **openMCsquare**, providing secondary Monte Carlo dose verification, daily CBCT Deformable Image Registration (DIR) Synthetic CT generation, per-fraction delivery log reconstruction, and AAPM TG-218 compliant 3D gamma analysis.

---

## Key Clinical Features

* **Secondary Dose Verification (openMCsquare)**: Fast 3D Monte Carlo dose recalculation on patient planning CTs and synthetic CTs using multi-threaded Linux SIMD binaries (AVX-512, AVX2, AVX, SSE4).
* **Synthetic CT & Adaptive QA (DIR)**: Ingests daily CBCT scans and spatial registration (REG) objects to generate synthetic CT volumes for fraction-specific anatomical dose recalculation.
* **Fractional Delivery Log QA**: Reconstructs 3D dose from machine delivery logs (RT Ion Treatment Records / `RI*.dcm`), tracking 6-DoF couch shifts, meterset accuracy, and spot position deviations.
* **Interrupted Delivery Flagging & Re-upload**: Detects early beam terminations and meterset shortfalls; allows physicists to upload merged DICOM records to recalculate 3D gamma.
* **Orthanc PACS / DICOM VNA Integration**: Built-in DICOMWeb query and retrieval of patient plans, planning CTs, delivery records, and CBCTs directly from Orthanc servers.
* **AAPM TG-218 Compliant Gamma Analysis**: 3D gamma index calculation (MC vs. TPS, Log vs. Rx, Synthetic CT vs. TPS) with customizable distance-to-agreement (DTA), dose difference (DD), and low-dose thresholding.
* **Automated Clinical Reporting**: Generates interactive web reports and print-ready PDFs (powered by WeasyPrint).

---

## System Requirements & Prerequisites

* **Operating System**: Linux (tested on Arch Linux, Ubuntu 22.04+, Debian 12+, Fedora 38+, RHEL / Rocky Linux 9+).
* **CPU**: x86_64 CPU with AVX2 or AVX support (OpenMP multi-threading enabled).
* **Python**: Python 3.10, 3.11, 3.12, or 3.14.
* **Node.js**: Node.js 18+ and npm (only needed if building the frontend from source).
* **System Libraries**:
  * OpenMP runtime: `libgomp`
  * PDF generation (WeasyPrint): `pango`, `cairo`, `gdk-pixbuf`, `libffi`

### Installing System Dependencies

#### Arch Linux:
```bash
sudo pacman -S python nodejs npm pango cairo gdk-pixbuf2 libffi gcc
```

#### Ubuntu / Debian:
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nodejs npm \
    libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libcairo2 \
    libgdk-pixbuf-2.0-0 libffi-dev build-essential
```

#### Fedora / RHEL / Rocky Linux:
```bash
sudo dnf install -y python3 python3-devel nodejs npm \
    pango cairo gdk-pixbuf2 libffi-devel gcc
```

---

## Quick Start (Automated Launcher)

The repository includes a self-configuring Linux launcher script [`run.sh`](run.sh):

```bash
# 1. Clone the repository
git clone https://github.com/apholt/virtual-psqa-open.git
cd virtual-psqa-open

# 2. Make the launcher executable
chmod +x run.sh

# 3. Launch Virtual PSQA
./run.sh
```

### What `./run.sh` Does Automatically:
1. Detects or creates a Python virtual environment (`.venv_linux`).
2. Installs or verifies all Python dependencies from `requirements.txt`.
3. Verifies if the React web frontend (`frontend/dist/`) is compiled, and runs `npm install && npm run build` if missing.
4. Sets executable permissions (`chmod +x`) on all Linux openMCsquare binaries (`MCsquare/MCsquare_linux*`).
5. Normalizes line endings on Monte Carlo materials and configuration files (`\r\n` $\rightarrow$ `\n`).
6. Starts the FastAPI server at **`http://localhost:8000`** with live hot-reloading.

---

## Manual Installation & Development Setup

If you prefer to configure your environment manually:

### 1. Set Up Python Virtual Environment

Using standard Python `venv`:
```bash
python3 -m venv .venv_linux
source .venv_linux/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Or using [`uv`](https://github.com/astral-sh/uv) (ultra-fast):
```bash
uv venv .venv_linux
source .venv_linux/bin/activate
uv pip install -r requirements.txt
```

### 2. Build the React Frontend

```bash
cd frontend
npm install
npm run build
cd ..
```
*This compiles production assets into `frontend/dist/`, which FastAPI serves statically.*

### 3. Ensure openMCsquare Linux Binary Permissions

```bash
chmod +x MCsquare/MCsquare_linux*
chmod +x MCsquare/MCsquare_double
```

### 4. Configure Environment Variables

Copy the example configuration to `.env`:
```bash
cp backend/.env.example backend/.env
```

Edit `backend/.env` to configure your Linux paths:
```ini
# openMCsquare Linux executable selection:
# Options: MCsquare_linux_avx512, MCsquare_linux_avx2, MCsquare_linux_avx, MCsquare_linux_sse4
MCSQUARE_HOME=../MCsquare
MCSQUARE_BINARY=../MCsquare/MCsquare_linux_avx2
MCSQUARE_BDL_PATH=../MCsquare/BDL
MCSQUARE_BDL_FILE=../MCsquare/BDL/GantryTrial5.txt
MCSQUARE_BDL_NAME=auto

# CPU Threading (0 = auto-detect all cores)
MCSQUARE_NUM_THREADS=0

# Local Storage (relative to backend/)
DICOM_STORE_PATH=./data/dicom_store
RESULTS_PATH=./data/results
DATABASE_URL=sqlite:///./data/psqa.db

# Optional: Watch Folder for automatic TPS exports
# DICOM_WATCH_FOLDER=/mnt/tps_exports/incoming
DICOM_SETTLE_SECONDS=5

# Optional: Orthanc PACS Server Integration
ORTHANC_URL=http://localhost:8042
ORTHANC_USERNAME=
ORTHANC_PASSWORD=
ORTHANC_TIMEOUT_SECONDS=120
```

### 5. Managing Users & Passwords (`manage_users.py`)

Manage clinical user accounts and passwords directly via the CLI (**HIPAA § 164.312(a)(2)(i)**):

```bash
# List all accounts
python backend/manage_users.py list

# Add a clinical user (physicist, dosimetrist, admin, auditor)
python backend/manage_users.py add jdoe "StrongPassword123!" --name "Dr. Jane Doe" --role physicist

# Update a password
python backend/manage_users.py passwd admin "NewAdminPassword2026!"

# Deactivate an account
python backend/manage_users.py deactivate jdoe
```
*(The script automatically detects and runs within `.venv_linux` even if invoked from system Python).*

### 6. Launch the Application

```bash
# Using the automated launcher (with automatic TLS/HTTPS certificates)
./run.sh

# Or launching manually via uvicorn
cd backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open your browser to:
* **Local Web UI**: [https://localhost:8000](https://localhost:8000) (or `http://localhost:8000` if SSL is disabled)
* **Interactive API Docs (Swagger)**: [https://localhost:8000/docs](https://localhost:8000/docs)

---

## Selecting the Optimal MCsquare Binary for Your CPU

openMCsquare achieves optimal calculation speeds when matching your CPU's vector instructions:

Check your CPU flags:
```bash
grep -E --color=auto "avx512|avx2|avx|sse4_1" /proc/cpuinfo | head -n 1
```

| Instruction Set | Binary Name | Typical Architecture | Performance |
| :--- | :--- | :--- | :--- |
| **AVX-512** | `MCsquare/MCsquare_linux_avx512` | Intel Xeon Scalable, Core i9 11th Gen+, AMD Zen 4+ | Highest |
| **AVX2** | `MCsquare/MCsquare_linux_avx2` | Intel Haswell (2013)+, AMD Zen 1/2/3/4 | Standard / Recommended |
| **AVX** | `MCsquare/MCsquare_linux_avx` | Intel Sandy Bridge (2011)+ | Fallback |
| **SSE4** | `MCsquare/MCsquare_linux_sse4` | Legacy / Virtual Machines without AVX | Compatibility |

Set `MCSQUARE_BINARY` in `backend/.env` to the binary matched to your hardware.

---

## Integrating with Orthanc PACS on Linux

Virtual PSQA can search and ingest directly from any DICOM PACS via Orthanc:

### Running Orthanc via Docker (Recommended for testing):
```bash
docker run -p 8042:8042 -p 4242:4242 --rm \
  -e ORTHANC__DICOM_WEB__ENABLE=true \
  jodogne/orthanc
```

### Connecting in Virtual PSQA:
1. Navigate to **Settings** $\rightarrow$ **Orthanc PACS Integration** in the web UI.
2. Set Server URL (e.g. `http://localhost:8042` or `http://pacs.clinic.lan:8042`).
3. Enter credentials (if authentication is enabled on Orthanc).
4. Click **Test Orthanc Connection**.
5. Once connected, use the **Import from Orthanc** button on the Dashboard or in the plan view to browse patients, download RT Plans, RT Records, and daily CBCT setups.

---

## Running the Automated Test Suite

All unit and integration tests can be run using `pytest`:

```bash
source .venv_linux/bin/activate
pytest backend/tests/ -v
```

The test suite covers:
* DICOM plan ingestion and Beam Data Library (BDL) matching
* openMCsquare simulation execution
* AAPM TG-218 3D gamma calculation engine
* Interrupted beam delivery detection and merged record re-upload
* Orthanc PACS integration endpoints
* Synthetic CT deformable registration and adaptive dose calculation

---

## Production Deployment as a Systemd Service

To run Virtual PSQA as a persistent background service on a clinical Linux server or workstation:

### 1. Create Systemd Service File
```bash
sudo nano /etc/systemd/system/virtual-psqa.service
```

Paste the following configuration (replace `/path/to/virtual-psqa-open` and `YOUR_USERNAME` with your actual system user and repository path):
```ini
[Unit]
Description=Virtual PSQA Clinical Radiation Therapy QA Service
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/virtual-psqa-open/backend
Environment="PATH=/path/to/virtual-psqa-open/.venv_linux/bin:/usr/local/bin:/usr/bin"
Environment="PYTHONPATH=/path/to/virtual-psqa-open/backend"
ExecStart=/path/to/virtual-psqa-open/.venv_linux/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 2. Enable and Start the Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable virtual-psqa
sudo systemctl start virtual-psqa
```

### 3. Check Status & View Logs
```bash
sudo systemctl status virtual-psqa
journalctl -u virtual-psqa -f
```

---

## Zero-PHI Data Architecture & Privacy

* **Strict PHI Protection**: All imported patient DICOM files, reconstructed doses, and SQLite tables reside strictly in `backend/data/` (configured in [`.gitignore`](.gitignore)).
* **Clean Initialization**: When cloned or downloaded fresh, the application automatically initializes an empty database schema (`backend/data/psqa.db`) on first startup.
* **Air-gapped Compatibility**: Virtual PSQA requires no external cloud connections and runs entirely on your local clinic network or workstation.

---

## Comprehensive Documentation

For full operations, multi-platform guidance, Orthanc integration, and HIPAA user management, please refer to:
- [**`DEPLOYMENT.md`**](DEPLOYMENT.md) — Comprehensive operations, updating, and service deployment guide.
- [**`MANUAL.md`**](MANUAL.md) — Complete clinical user manual and theory reference.
- [**`CITATIONS.md`**](CITATIONS.md) — Academic citations and scientific acknowledgments.

---

## Authors & Primary Contributors

* **Aaron Hutchins** ([ahutchins180@gmail.com](mailto:ahutchins180@gmail.com)) — **Lead Developer & System Architect**
* **Adam Holt** ([sebaldus.adam@gmail.com](mailto:sebaldus.adam@gmail.com) / [@apholt](https://github.com/apholt)) — **Contributor**

---

## Citations & Open-Source Acknowledgments

Virtual PSQA utilizes several foundational open-source medical physics projects and scientific packages, including **openMCsquare**, **Orthanc**, **SimpleITK**, and **pydicom**. Please refer to [**`CITATIONS.md`**](CITATIONS.md) for full academic citations and BibTeX records.
