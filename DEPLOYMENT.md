# Virtual PSQA — Deployment Guide

## Prerequisites

- Windows 10/11 workstation on the clinic LAN
- Python 3.11+ installed
- Node.js 18+ installed (for the initial frontend build only)

---

## 1. Install Python dependencies

```powershell
cd virtual-psqa
pip install -r requirements.txt
```

---

## 2. Build the React frontend

```powershell
cd frontend
npm install
npm run build
cd ..
```

This outputs static files to `frontend/dist/`. FastAPI serves these automatically.

---

## 3. Configure the application

Create a `.env` file in `backend/` to override defaults:

```env
# Authentication & HIPAA Security Safeguards
AUTH_ENABLED=true
AUTH_USERNAME=admin
AUTH_PASSWORD=your-secure-password
AUTH_SESSION_EXPIRE_MINUTES=15

# Transmission Security (§ 164.312(e)) - TLS/HTTPS
SSL_ENABLED=true
SSL_KEYFILE=./certs/key.pem
SSL_CERTFILE=./certs/cert.pem

# P drive folder RayStation exports to (optional — enables auto-ingestion)
DICOM_WATCH_FOLDER=P:\PSQA_incoming

# Paths (defaults work if you run from the project root)
DICOM_STORE_PATH=./data/dicom_store
RESULTS_PATH=./data/results

# MCsquare binary (Phase 2)
MCSQUARE_BINARY=./mcSquare/MCsquare
MCSQUARE_BDL_PATH=./mcSquare/BDL

# Orthanc PACS / VNA Server Integration (optional)
ORTHANC_URL=http://localhost:8042
ORTHANC_USERNAME=
ORTHANC_PASSWORD=
ORTHANC_TIMEOUT_SECONDS=120
```

### HIPAA User Management (§ 164.312(a)(2)(i))

To manage unique clinical user accounts and passwords, run the CLI utility (works from repository root as `python backend/manage_users.py` or inside `backend/` with automatic virtualenv detection):

```bash
# Add a medical physicist (roles: physicist, dosimetrist, admin, auditor)
python manage_users.py add jdoe "StrongPassword123!" --name "Dr. John Doe" --role physicist

# List registered clinical accounts
python manage_users.py list

# Update password
python manage_users.py passwd jdoe "NewPassword456!"

# Deactivate an account
python manage_users.py deactivate jdoe
```

---

## 4. Initialize the database

```powershell
cd backend
alembic upgrade head
cd ..
```

---

## 5. Start the server (manual / dev)

```powershell
cd backend
python main.py
```

The app is now accessible at `http://localhost:8000` locally, and from any clinic
machine at `http://<workstation-LAN-IP>:8000`.

Find your LAN IP:
```powershell
ipconfig
```
Look for the IPv4 address on your network adapter (e.g. `192.168.1.105`).

---

## 6. Open Windows Firewall (one-time)

Run this once in an **elevated** (Admin) PowerShell to allow clinic machines
to connect on port 8000:

```powershell
New-NetFirewallRule `
  -DisplayName "Virtual PSQA" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 8000 `
  -Action Allow
```

---

## 7. Install as a Windows Service with NSSM

This makes the app start automatically on boot without needing to log in.

1. Download NSSM from https://nssm.cc and place `nssm.exe` in the project root.

2. Run these commands in an **elevated** PowerShell (replace paths as needed):

```powershell
$pythonPath = (Get-Command python).Source
$projectPath = "C:\path\to\virtual-psqa"
$backendPath = "$projectPath\backend"

.\nssm.exe install VirtualPSQA "$pythonPath" "$backendPath\main.py"
.\nssm.exe set VirtualPSQA AppDirectory "$backendPath"
.\nssm.exe set VirtualPSQA DisplayName "Virtual PSQA Server"
.\nssm.exe set VirtualPSQA Description "Proton PBS virtual QA application"
.\nssm.exe set VirtualPSQA Start SERVICE_AUTO_START
.\nssm.exe set VirtualPSQA AppStdout "$projectPath\logs\service.log"
.\nssm.exe set VirtualPSQA AppStderr "$projectPath\logs\service_error.log"
Start-Service VirtualPSQA
```

3. To manage the service:

```powershell
Start-Service VirtualPSQA
Stop-Service VirtualPSQA
Restart-Service VirtualPSQA
Get-Service VirtualPSQA
```

---

## 8. P drive folder watcher

Set `DICOM_WATCH_FOLDER` in `.env` to the path RayStation exports to:

```env
DICOM_WATCH_FOLDER=P:\PSQA_incoming
```

RayStation should be configured to export plan files into patient-named subfolders:

```
P:\PSQA_incoming\
├── PT_2024_0347_HN\
│   ├── RP.1.2.xxx.dcm        # RTIonPlan
│   ├── RD.1.2.xxx.dcm        # RTDose
│   ├── RS.1.2.xxx.dcm        # RTStruct
│   └── RI.1.2.xxx.dcm        # RT Ion Record (fraction log)
└── PT_2024_0351_Prostate\
    └── ...
```

Files are auto-ingested and the worklist updates in real time (no page refresh needed).

---

## Updating the application

```powershell
# Pull latest code
git pull

# Rebuild frontend
cd frontend
npm run build
cd ..

# Apply any new database migrations
cd backend
alembic upgrade head
cd ..

# Restart the service
Restart-Service VirtualPSQA
```

---

## ML prediction engine

The app fuses four evidence layers (plan complexity, MCsquare gamma, log
reconstruction gamma, physical measurement) into a calibrated pass probability
and verdict (`virtual_approve` / `flag` / `measure`).

- **Bootstrap mode:** until `ML_RETRAIN_MIN_CASES` (default 50) plans have a
  recorded physical outcome, every plan returns `measure` / `low` confidence.
  This is the correct conservative cold-start behaviour.
- **Recording outcomes:** open a plan → **Record physical QA outcome** (pass/fail
  + optional measured passing rate). Each outcome becomes a training example.
- **Retraining:** Model insights → **Retrain now** (or `POST /api/ml/retrain`).
  The algorithm auto-upgrades by sample count: logistic regression (<200) →
  random forest (<500) → gradient boosting (≥500).
- **Model files** live in `backend/ml/models/` (`model.joblib`, `model_meta.json`,
  `model_history.json`).

**Reset the model** (return to bootstrap): stop the service and delete the files
in `backend/ml/models/`.

**Reset the database** (wipe all plans/predictions): stop the service, then from
`backend/` run:

```powershell
python -c "import models; from database import Base, engine; Base.metadata.drop_all(bind=engine); Base.metadata.create_all(bind=engine)"
```

**Back up the database:** copy `data/psqa.db` (and optionally `data/results/`
and `backend/ml/models/`) while the service is stopped.

## Reports

Open a plan → **Report** (or `GET /api/reports/{plan_id}`) for a print-ready
clinical report. Use the browser's **Save as PDF** for an archival copy.

For true server-side PDF (`GET /api/reports/{plan_id}?format=pdf`), install
WeasyPrint **and** the GTK runtime (Pango/Cairo). Without it the endpoint
gracefully returns the same report as HTML — no GTK install required on Windows.

---

## Troubleshooting

| Issue | Check |
|---|---|
| App not reachable from other machines | Run the firewall rule (Step 6) |
| DICOM files not auto-ingested | Check `DICOM_WATCH_FOLDER` path; use `PollingObserver` (already used) |
| Database errors | Re-run `alembic upgrade head` |
| Frontend not loading | Re-run `npm run build` in `frontend/` |
| Service not starting | Check `logs/service_error.log` |
| All verdicts are "measure" | Expected until ≥ 50 outcomes recorded (bootstrap mode) |
| PDF report returns HTML | WeasyPrint/GTK not installed — use browser Save as PDF |
