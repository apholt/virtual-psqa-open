# Virtual PSQA — Windows Deployment & User Guide

Virtual Patient-Specific Quality Assurance (PSQA) platform for proton pencil beam scanning (PBS) radiotherapy, featuring openMCsquare Monte Carlo dose recalculation, Deformable Image Registration (DIR) synthetic CT generation from CBCTs, log file reconstruction, and AAPM TG-218 compliant 3D gamma analysis.

---

## Zero-Dependency Quick Start

This package is **completely portable and self-contained**. It includes a bundled Windows Python runtime, pre-compiled openMCsquare Monte Carlo binaries, and the pre-built web application. **No system Python, pip, or Node.js installation is required.**

### 1. Extract the Archive
Extract `virtual-psqa-windows.zip` into any folder on your Windows machine, for example:
- `C:\VirtualPSQA\` or
- `C:\Users\<YourUsername>\Desktop\virtual-psqa\`

> [!NOTE]
> Avoid folder paths with special characters or excessive nesting.

### 2. Launch the Application
Double-click **`run.bat`** (or **`start.bat`**).

A console window will appear:
```text
============================================================
 Virtual PSQA Server
============================================================
 Local UI:    http://localhost:8000
 Network UI:  http://YOUR-COMPUTER-NAME:8000
 Python:      C:\VirtualPSQA\python\python.exe

 Press Ctrl+C to stop the server.
============================================================
```

### 3. Open in Browser
Navigate to **`http://localhost:8000`** in Google Chrome, Microsoft Edge, or Firefox.
From any other computer on your clinic LAN, you can navigate to `http://<YOUR-IP-OR-COMPUTERNAME>:8000`.

To stop the server at any time, press `Ctrl + C` in the console window.

---

## Database & Privacy Notice (Zero Patient Data)

- This deployment contains **NO patient records, NO DICOM images, and NO Protected Health Information (PHI)**.
- On the first launch, the application automatically initializes a clean, empty database (`backend/data/psqa.db`) and sets up internal storage directories.
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

## Configuration & Customization (Optional)

Settings are managed in `backend/.env`. If the file does not exist, it is automatically created from `backend/.env.example` on first run.

Key settings you can customize:
- `DICOM_WATCH_FOLDER`: Point this to a network share or local folder (e.g. `P:\PSQA_incoming`) where RayStation/TPS exports plans for automatic background ingestion.
- `MCSQUARE_EXE`: Defaults to `MCsquare_win_avx2.exe`. If your CPU does not support AVX2, you can change this to `MCsquare_win.exe`, `MCsquare_win_avx.exe`, or `MCsquare_win_sse4.exe`.
- `MCSQUARE_PRIMARIES`: Number of proton histories for Monte Carlo simulation (default `10,000,000`).
- `MCSQUARE_STAT_UNCERTAINTY`: Target statistical uncertainty in % (default `1.5`).
- `GAMMA_MCSQUARE_VS_TPS_DD`: Dose difference tolerance (default `3.0`%).
- `GAMMA_MCSQUARE_VS_TPS_DTA`: Distance-to-agreement tolerance (default `3.0` mm).
- `GAMMA_MCSQUARE_VS_TPS_THRESHOLD`: Gamma passing threshold (default `90.0`%).

---

## Windows Firewall (LAN Access)

If clinical workstations on your network cannot access `http://<server-ip>:8000`, run this single command in an **Administrator PowerShell** window:

```powershell
New-NetFirewallRule -DisplayName "Virtual PSQA" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

---

## Running as a Persistent Windows Service (Optional)

To start Virtual PSQA automatically when the machine boots without needing a user to log in:

1. Download **NSSM** (Non-Sucking Service Manager) from `https://nssm.cc` and extract `nssm.exe`.
2. Open an **Administrator PowerShell** and run (adjust paths to match your folder):

```powershell
$root = "C:\VirtualPSQA"
$py = "$root\python\python.exe"
$backend = "$root\backend"

.\nssm.exe install VirtualPSQA "$py" "-m uvicorn main:app --host 0.0.0.0 --port 8000"
.\nssm.exe set VirtualPSQA AppDirectory "$backend"
.\nssm.exe set VirtualPSQA DisplayName "Virtual PSQA Server"
.\nssm.exe set VirtualPSQA Description "Virtual Patient-Specific QA Platform"
.\nssm.exe set VirtualPSQA Start SERVICE_AUTO_START
Start-Service VirtualPSQA
```

---

## Authors & Primary Contributors

* **Aaron Hutchins** ([ahutchins@tnonc.com](mailto:ahutchins@tnonc.com)) — **Lead Developer & System Architect**
* **Adam Holt** ([sebaldus.adam@gmail.com](mailto:sebaldus.adam@gmail.com) / [@apholt](https://github.com/apholt)) — **Contributor**

For complete scientific citations and references, please see [`CITATIONS.md`](CITATIONS.md).
