# Virtual PSQA — Windows Deployment & User Guide

Virtual Patient-Specific Quality Assurance (PSQA) platform for proton pencil beam scanning (PBS) radiotherapy, featuring openMCsquare Monte Carlo dose recalculation, Deformable Image Registration (DIR) synthetic CT generation from CBCTs, log file reconstruction, and AAPM TG-218 compliant 3D gamma analysis.

---

## Installation & Quick Start

You can install and run Virtual PSQA on Windows via either **Git Clone (Source)** or the **Portable Pre-built Package**.

### Option A: Installation via Git & CLI (Recommended)

#### 1. Prerequisites
- **Python 3.11+** ([python.org](https://www.python.org))
- **Node.js 18+ LTS** ([nodejs.org](https://nodejs.org))
- **Git for Windows** ([git-scm.com](https://git-scm.com))

> [!TIP]
> Both Python and Node.js can be installed in seconds via `winget` in PowerShell:
> ```powershell
> winget install -e --id Python.Python.3.11
> winget install -e --id OpenJS.NodeJS.LTS
> ```

#### 2. Clone the Repository
```powershell
git clone https://github.com/apholt/virtual-psqa-open.git
cd virtual-psqa-open
```

#### 3. Run Automated Setup
Run the bundled setup script:
```cmd
setup.bat
```
*(or `.\setup.bat` in PowerShell)*

This automatically creates the `.venv` virtual environment, installs all Python packages from `requirements.txt`, compiles the React frontend into `frontend\dist`, and initializes `backend\.env`.

#### 4. Launch Virtual PSQA
```cmd
run.bat
```
*(or `start.bat`)*

---

### Option B: Standalone Portable Package (`virtual-psqa-windows.zip`)

If you downloaded the standalone offline zip:
1. Extract `virtual-psqa-windows.zip` into any folder (e.g., `C:\VirtualPSQA\`).
2. Double-click **`run.bat`** (or **`start.bat`**).
*(No system Python, pip, or Node.js installation is required for this package).*

---

### 3. Open in Browser
Navigate to **`http://localhost:8000`** in Google Chrome, Microsoft Edge, or Firefox.
From any other computer on your clinic LAN, navigate to `http://<YOUR-IP-OR-COMPUTERNAME>:8000`.

To stop the server at any time, press `Ctrl + C` in the console window.

---

## Database & Privacy Notice (Zero Patient Data)

- This deployment contains **NO patient records, NO DICOM images, and NO Protected Health Information (PHI)**.
- On first launch, the application automatically initializes a clean, empty database (`backend/data/psqa.db`) and sets up internal storage directories.
- All subsequent patient data, synthetic CTs, and calculation results remain strictly local in `backend/data/`.

---

## Getting Started: Uploading & Calculating

1. **Upload Plan**:
   - In the web UI, click **Upload Plan** (or go to `http://localhost:8000/plans/upload`).
   - Drag and drop your DICOM export folder containing:
     - `RP*.dcm` (RTIonPlan)
     - `RD*.dcm` (RTDose from TPS)
     - `RS*.dcm` (RTStructureSet)
     - `CT*.dcm` (Planning CT slices)
     - Optional: `RI*.dcm` (RT Ion Record delivery log)
   - The system validates and indexes the plan automatically.

2. **Run Secondary Dose Calculation (MCsquare)**:
   - Open the plan from the Worklist dashboard.
   - Click **Run MCsquare Simulation** (or **Calculate Dose**).
   - Real-time progress is displayed.
   - You can cancel the calculation at any time by clicking the red **Cancel Simulation** button.

3. **Fraction Synthetic CT & Adaptive Dose**:
   - In the **Synthetic CT** tab, upload a daily CBCT series for any fraction.
   - Check the auto-segmented patient external contour with the interactive Window/Level tools.
   - Click **Calculate Dose** on the synthetic CT to assess anatomical changes and delivered dose.
   - Individual fraction dose calculations can also be cancelled immediately via the **Cancel Dose Calc** button.

4. **Upload RT Delivery Records**:
   - Single or batch RT Ion Record files (`RI*.dcm` / `.dcm`) can be uploaded at any time.
   - The system automatically reads the beam delivery logs, matches them to the corresponding patient and plan, and links them to the fraction.

---

## Configuration & Customization (`backend\.env`)

Settings are managed in `backend\.env`. If the file does not exist, it is automatically created from `backend\.env.example` on first run.

Key settings you can customize:
- `DICOM_WATCH_FOLDER`: Point this to a network share or local folder (e.g. `P:\PSQA_incoming`) where RayStation/TPS exports plans for automatic background ingestion.
- `MCSQUARE_BINARY`: Defaults to `..\MCsquare\MCsquare_win_avx2.exe`. If your CPU supports AVX-512, change this to `..\MCsquare\MCsquare_win_avx512.exe`; for legacy systems without AVX2, use `..\MCsquare\MCsquare_win_avx.exe` or `..\MCsquare\MCsquare_win_sse4.exe`.
- `MCSQUARE_PRIMARIES`: Number of proton histories for Monte Carlo simulation (default `10,000,000`).
- `MCSQUARE_STAT_UNCERTAINTY`: Target statistical uncertainty in % (default `1.5`).
- `GAMMA_MCSQUARE_VS_TPS_DD`: Dose difference tolerance (default `3.0`%).
- `GAMMA_MCSQUARE_VS_TPS_DTA`: Distance-to-agreement tolerance (default `3.0` mm).
- `GAMMA_MCSQUARE_VS_TPS_THRESHOLD`: Gamma passing threshold (default `90.0`%).

---

## Windows Firewall (LAN Access)

If clinical workstations on your network cannot access `http://<server-ip>:8000`, run this command in an **Administrator PowerShell** window:

```powershell
New-NetFirewallRule -DisplayName "Virtual PSQA" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

---

## Running as a Persistent Windows Service with NSSM

To start Virtual PSQA automatically when the machine boots without needing a user to log in:

1. Download **NSSM** (Non-Sucking Service Manager) from [https://nssm.cc](https://nssm.cc) and place `nssm.exe` in the project root folder.
2. Open an **Administrator PowerShell** and run (adjust `$root` to match your folder):

```powershell
$root = "C:\path\to\virtual-psqa-open"
$py = "$root\.venv\Scripts\python.exe"
$backend = "$root\backend"

.\nssm.exe install VirtualPSQA "$py" "-m uvicorn main:app --host 0.0.0.0 --port 8000"
.\nssm.exe set VirtualPSQA AppDirectory "$backend"
.\nssm.exe set VirtualPSQA DisplayName "Virtual PSQA Server"
.\nssm.exe set VirtualPSQA Description "Virtual Patient-Specific QA Platform"
.\nssm.exe set VirtualPSQA Start SERVICE_AUTO_START
.\nssm.exe set VirtualPSQA AppStdout "$root\logs\service.log"
.\nssm.exe set VirtualPSQA AppStderr "$root\logs\service_error.log"

Start-Service VirtualPSQA
```

---

## Comprehensive Documentation

For full operations, multi-platform guidance, Orthanc integration, and HIPAA user management, please refer to:
- [**`DEPLOYMENT.md`**](DEPLOYMENT.md) — Comprehensive operations, updating, and service deployment guide.
- [**`MANUAL.md`**](MANUAL.md) — Complete clinical user manual and theory reference.
- [**`CITATIONS.md`**](CITATIONS.md) — Academic citations and scientific acknowledgments.

---

## Authors & Primary Contributors

* **Aaron Hutchins** ([ahutchins180@gmail.com](mailto:ahutchins180@gmail.com)) — **System Architect**  
  *Principal author who inspired, designed, and built the core codebase, openMCsquare integration, delivery log reconstruction, and clinical QA decision engine.*
* **Adam Holt** ([sebaldus.adam@gmail.com](mailto:apholt21@outlook.com) / [@apholt](https://github.com/apholt)) — **Lead Developer & Contributor**  
  *Codebase improvements, quality-of-life features, ORTHANC integration, HIPAA security requirements, openMCsquare scenario robustness and DVH prediction modules, Linux packaging, and open-source distribution.*
