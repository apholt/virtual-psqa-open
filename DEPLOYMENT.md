# Virtual PSQA — Deployment & Operations Guide

A comprehensive deployment and operations guide for **Virtual PSQA** (Patient-Specific Quality Assurance platform for proton pencil beam scanning radiotherapy).

Virtual PSQA is designed for flexible clinical deployment on:
1. **Windows 10/11 Workstations** (Primary clinic workstation or dedicated QA PC)
2. **Linux Servers / Workstations** (Ubuntu, Debian, Fedora, Arch, Rocky/RHEL)

---

## Quick Architecture Summary

Virtual PSQA consists of two main components:
- **Backend**: FastAPI Python application providing DICOM parsing, openMCsquare Monte Carlo simulation management, log file dose reconstruction, synthetic CT deformable image registration, and 3D gamma analysis.
- **Frontend**: Single-Page Application (React / Vite / Tailwind CSS) compiled to static files in `frontend/dist/` and served directly by FastAPI.

---

## Part 1: Windows Deployment

### Prerequisites
- **Operating System**: Windows 10 (64-bit) or Windows 11
- **Python**: Python 3.11+ ([python.org](https://www.python.org))
- **Node.js**: Node.js 18+ LTS ([nodejs.org](https://nodejs.org)) *(only needed for the initial frontend build from source)*
- **Git**: Git for Windows ([git-scm.com](https://git-scm.com))

> [!TIP]
> **CLI Installation via `winget`:**  
> On Windows 10/11, you can install Python and Node.js directly from PowerShell:
> ```powershell
> winget install -e --id Python.Python.3.11
> winget install -e --id OpenJS.NodeJS.LTS
> ```
> *(Restart your terminal after installation so environment variables are refreshed)*.

---

### Method A: Automated Setup via `setup.bat` (Recommended)

1. **Clone the Repository**:
   ```cmd
   git clone https://github.com/apholt/virtual-psqa-open.git
   cd virtual-psqa-open
   ```

2. **Run the Setup Script**:
   ```cmd
   setup.bat
   ```
   *(or `.\setup.bat` in PowerShell)*

   **What `setup.bat` executes automatically:**
   1. Detects Python 3.11+ and creates a local virtual environment in `.venv\`.
   2. Upgrades `pip` and installs all dependencies from `requirements.txt`.
   3. Installs npm dependencies and compiles the React application into `frontend\dist\`.
   4. Generates `backend\.env` from `backend\.env.example` if not already present.

3. **Launch Virtual PSQA**:
   ```cmd
   run.bat
   ```
   *(or `start.bat`)*

4. **Access the Web Dashboard**:
   - Open your browser to `http://localhost:8000`.
   - From any clinic PC on the same LAN: `http://<SERVER-IP-OR-HOSTNAME>:8000`.

> [!NOTE]
> **If `npm install` appears to freeze in console:**
> Windows Command Prompt and PowerShell enable "QuickEdit Mode" by default. Clicking anywhere inside the window pauses output and background tasks. Press **`Enter`** or **`Esc`** in the console window to resume.  
> Alternatively, if the Windows machine lacks internet access, you can copy the pre-built `frontend\dist` folder from another machine directly into `frontend\dist\`, and `setup.bat` will skip the npm build.

---

### Method B: Manual Step-by-Step Setup (PowerShell)

If you prefer to configure the environment by hand:

```powershell
# 1. Clone repository
git clone https://github.com/apholt/virtual-psqa-open.git
cd virtual-psqa-open

# 2. Create and activate Python virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 3. Install Python dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# 4. Build the web frontend
cd frontend
npm install
npm run build
cd ..

# 5. Initialize configuration
copy backend\.env.example backend\.env

# 6. Start the server
cd backend
python main.py
```

*(On first startup, the application automatically initializes the SQLite schema in `backend\data\psqa.db` and ensures initial admin accounts—no manual migration command is required.)*

---

### Windows Firewall Configuration (LAN Access)

To allow other clinical computers or workstations on your LAN to access the web UI and REST API on port 8000, run this command once in an **Administrator PowerShell**:

```powershell
New-NetFirewallRule -DisplayName "Virtual PSQA" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

---

### Running as a Persistent Windows Service (NSSM)

To run Virtual PSQA automatically when the workstation boots (without requiring a user to log in):

1. Download **NSSM** (Non-Sucking Service Manager) from [https://nssm.cc](https://nssm.cc) and place `nssm.exe` in the project root folder.
2. Open an **Administrator PowerShell** and run:

```powershell
$projectPath = "C:\path\to\virtual-psqa-open"
$py = "$projectPath\.venv\Scripts\python.exe"
$backend = "$projectPath\backend"

.\nssm.exe install VirtualPSQA "$py" "-m uvicorn main:app --host 0.0.0.0 --port 8000"
.\nssm.exe set VirtualPSQA AppDirectory "$backend"
.\nssm.exe set VirtualPSQA DisplayName "Virtual PSQA Server"
.\nssm.exe set VirtualPSQA Description "Virtual Patient-Specific QA Platform"
.\nssm.exe set VirtualPSQA Start SERVICE_AUTO_START
.\nssm.exe set VirtualPSQA AppStdout "$projectPath\logs\service.log"
.\nssm.exe set VirtualPSQA AppStderr "$projectPath\logs\service_error.log"

Start-Service VirtualPSQA
```

To manage the service:
```powershell
Start-Service VirtualPSQA
Stop-Service VirtualPSQA
Restart-Service VirtualPSQA
Get-Service VirtualPSQA
```

---

## Part 2: Linux Deployment

### System Prerequisites
Tested on Ubuntu 22.04+, Debian 12+, Arch Linux, Fedora 38+, Rocky/RHEL 9+.

Install required system libraries (OpenMP runtime and WeasyPrint dependencies):
- **Ubuntu / Debian**:
  ```bash
  sudo apt update && sudo apt install -y python3 python3-venv python3-pip nodejs npm \
      libpango-1.0-0 libharfbuzz0b libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev build-essential
  ```
- **Arch Linux**:
  ```bash
  sudo pacman -S python nodejs npm pango cairo gdk-pixbuf2 libffi gcc
  ```
- **Fedora / RHEL / Rocky Linux**:
  ```bash
  sudo dnf install -y python3 python3-devel nodejs npm pango cairo gdk-pixbuf2 libffi-devel gcc
  ```

---

### Automated Linux Launcher (`run.sh`)

```bash
# 1. Clone repository
git clone https://github.com/apholt/virtual-psqa-open.git
cd virtual-psqa-open

# 2. Make executable & run
chmod +x run.sh
./run.sh
```

**What `./run.sh` does:**
- Detects or initializes `.venv_linux/`.
- Verifies Python dependencies.
- Compiles `frontend/dist/` if missing.
- Sets executable permissions on `MCsquare/MCsquare_linux*`.
- Normalizes CRLF line endings on Monte Carlo materials and BDL files.
- Launches the FastAPI server at `http://localhost:8000`.

---

### Running as a Persistent Systemd Service (Linux)

1. Create a service definition file:
   ```bash
   sudo nano /etc/systemd/system/virtual-psqa.service
   ```

2. Add the configuration (adjust `/path/to/virtual-psqa-open` and user):
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

3. Enable and start:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable virtual-psqa
   sudo systemctl start virtual-psqa
   ```

---

## Part 3: Environment Configuration (`backend/.env`)

Settings are loaded from `backend/.env`. If the file does not exist, copy it from `backend/.env.example`.

### Hardware & openMCsquare Binary Matching

Match the binary to your processor's vector instruction set:

#### On Windows:
```env
MCSQUARE_HOME=..\MCsquare
MCSQUARE_BINARY=..\MCsquare\MCsquare_win_avx2.exe
MCSQUARE_BDL_PATH=..\MCsquare\BDL
MCSQUARE_BDL_FILE=..\MCsquare\BDL\GantryTrial5.txt
MCSQUARE_BDL_NAME=auto
```
*Options:* `MCsquare_win_avx512.exe`, `MCsquare_win_avx2.exe` (recommended), `MCsquare_win_avx.exe`, `MCsquare_win_sse4.exe`.

#### On Linux:
```env
MCSQUARE_HOME=../MCsquare
MCSQUARE_BINARY=../MCsquare/MCsquare_linux_avx2
MCSQUARE_BDL_PATH=../MCsquare/BDL
MCSQUARE_BDL_FILE=../MCsquare/BDL/GantryTrial5.txt
MCSQUARE_BDL_NAME=auto
```
*Options:* `MCsquare_linux_avx512`, `MCsquare_linux_avx2` (recommended), `MCsquare_linux_avx`, `MCsquare_linux_sse4`.

---

### Automated Ingestion via DICOM Watch Folder

Configure the incoming folder where RayStation or your primary TPS exports plans:

```env
# Windows mapped drive or local path:
DICOM_WATCH_FOLDER=P:\PSQA_incoming
DICOM_SETTLE_SECONDS=5

# Linux mount:
# DICOM_WATCH_FOLDER=/mnt/tps_exports/incoming
```

Incoming plans (with `RP*.dcm`, `RD*.dcm`, `RS*.dcm`, `CT*.dcm`) or fraction delivery logs (`RI*.dcm`) are detected, parsed, and indexed automatically in real time.

---

### Orthanc PACS / DICOMWeb Integration (Optional)

```env
ORTHANC_URL=http://localhost:8042
ORTHANC_USERNAME=
ORTHANC_PASSWORD=
ORTHANC_TIMEOUT_SECONDS=120
```

---

## Part 4: Clinical User Management (HIPAA § 164.312(a)(2)(i))

The included CLI utility `backend/manage_users.py` manages clinical user accounts with role-based access control (roles: `physicist`, `dosimetrist`, `admin`, `auditor`).

### Windows:
```powershell
# List accounts
.\.venv\Scripts\python.exe backend\manage_users.py list

# Add a user
.\.venv\Scripts\python.exe backend\manage_users.py add jdoe "SecurePassword123!" --name "Dr. Jane Doe" --role physicist

# Change password
.\.venv\Scripts\python.exe backend\manage_users.py passwd jdoe "NewSecurePassword456!"

# Deactivate account
.\.venv\Scripts\python.exe backend\manage_users.py deactivate jdoe
```

### Linux:
```bash
python backend/manage_users.py list
python backend/manage_users.py add jdoe "SecurePassword123!" --name "Dr. Jane Doe" --role physicist
python backend/manage_users.py passwd jdoe "NewSecurePassword456!"
python backend/manage_users.py deactivate jdoe
```

---

## Part 5: Updating the Application

### On Windows:
```cmd
git pull
setup.bat
# If running as an NSSM service:
powershell Restart-Service VirtualPSQA
```

### On Linux:
```bash
git pull
./run.sh
# If running as a systemd service:
sudo systemctl restart virtual-psqa
```

---

## Troubleshooting Guide

| Issue | Likely Cause | Solution |
| :--- | :--- | :--- |
| `npm install` appears frozen on Windows | Windows console QuickEdit mode paused output | Click into console and press `Enter` or `Esc`. |
| `ERROR: Web frontend build not found at frontend\dist\` | Web frontend has not been compiled | Run `setup.bat` (Windows) or `npm run build` in `frontend/`. |
| Cannot reach server from other clinic PCs | Windows Firewall blocking port 8000 | Run the `New-NetFirewallRule` command in Administrator PowerShell. |
| MCsquare crashes or outputs "Please verify processor supports..." | CPU lacks vector instructions for the configured binary | Edit `backend\.env` to select `_avx2`, `_avx`, or `_sse4`. |
| DICOM files not auto-ingested | Invalid watch folder path or permission | Verify `DICOM_WATCH_FOLDER` exists and has read permissions. |
| Python / pip command not found | Python not registered in `%PATH%` | Re-run installer with "Add Python to PATH" checked, or use `.\.venv\Scripts\python.exe`. |
