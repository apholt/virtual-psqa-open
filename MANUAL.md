# Virtual PSQA — Complete Operating Manual

**Proton Pencil-Beam-Scanning (PBS) Patient-Specific QA Application**

> **Document purpose.** This is a self-contained reference for the Virtual PSQA
> application: what it is, how every part works, how to install and configure it,
> how data flows through it, how to run it day to day, and how to keep it healthy.
> It is written so it can be read on its own (e.g. pasted into a web AI assistant)
> without needing the source code in front of you.
>
> **Audience:** the medical physicist / administrator running the app on a clinic
> Windows workstation.

---

## Table of contents

1. [What the application does (plain language)](#1-what-the-application-does-plain-language)
2. [Core concepts: the four evidence layers and the verdict](#2-core-concepts-the-four-evidence-layers-and-the-verdict)
3. [System architecture](#3-system-architecture)
4. [Project folder structure](#4-project-folder-structure)
5. [Prerequisites](#5-prerequisites-what-must-be-installed)
6. [First-time installation](#6-first-time-installation)
7. [Configuration — `backend\.env`](#7-configuration--backendenv-every-setting-explained)
8. [Starting and stopping the application](#8-starting-and-stopping-the-application)
9. [How data flows in: DICOM types and the two-stage pipeline](#9-how-data-flows-in-dicom-types-and-the-two-stage-pipeline)
10. [Evidence layer 1 — Plan complexity](#10-evidence-layer-1--plan-complexity)
11. [Evidence layer 2 — MCsquare secondary Monte-Carlo dose](#11-evidence-layer-2--mcsquare-secondary-monte-carlo-dose)
12. [Evidence layer 3 — Delivery log (RT Ion Record) reconstruction](#12-evidence-layer-3--delivery-log-rt-ion-record-reconstruction)
13. [Evidence layer 4 — Physical measurement (recorded outcome)](#13-evidence-layer-4--physical-measurement-recorded-outcome)
14. [Gamma analysis](#14-gamma-analysis-how-doses-are-compared)
15. [The machine-learning prediction engine](#15-the-machine-learning-prediction-engine)
16. [Using the web interface (page by page)](#16-using-the-web-interface-page-by-page)
17. [The REST API (endpoint reference)](#17-the-rest-api-endpoint-reference)
18. [The database, backups, and resets](#18-the-database-backups-and-resets)
19. [Reports](#19-reports)
20. [Updating the application](#20-updating-the-application)
21. [Troubleshooting](#21-troubleshooting)
22. [Clinical safety — read before relying on verdicts](#22-clinical-safety--read-before-relying-on-verdicts)
23. [Quick-start checklist](#23-quick-start-checklist)

---

## 1. What the application does (plain language)

Virtual PSQA is a decision-support tool for proton PBS patient-specific QA. Its
goal is to reduce (and eventually triage) the number of plans that require a
physical measurement, by gathering independent computational evidence that a plan
will deliver as intended.

For each proton plan it automatically:

1. Reads the plan from the treatment planning system (TPS) export (DICOM).
2. Computes plan-complexity metrics directly from the plan.
3. Runs an **independent** secondary Monte-Carlo dose calculation (openMCsquare)
   and compares it to the TPS dose with a gamma analysis.
4. After the patient is treated, reads the machine delivery log (RT Ion Record),
   reconstructs the delivered dose, and compares **that** to the TPS dose.
5. Optionally records the result of a real physical measurement.
6. Fuses all of the above into a single calibrated **pass probability** and a
   clinical verdict: **Virtual-Approve / Flag / Measure**.

It presents all of this in a web dashboard that any machine on the clinic LAN can
open in a browser — there is nothing to install on the client machines.

> **Important.** Out of the box the app runs in conservative *bootstrap* mode and
> a Monte-Carlo *simulation* mode. It is a data-collection and decision-support
> platform; it does **not** replace physical QA until a model has been trained and
> validated on your own clinic's outcomes (see [§15](#15-the-machine-learning-prediction-engine)
> and [§22](#22-clinical-safety--read-before-relying-on-verdicts)).

---

## 2. Core concepts: the four evidence layers and the verdict

The whole system is built around fusing **four independent evidence layers**:

| Layer | Name | What it is | When available |
|---|---|---|---|
| 1 | **Complexity** | Metrics from the plan alone (MCS, SAS, MU/Gy, spots/layer, energy range). No simulation. | Immediately at ingest |
| 2 | **MCsquare** | Independent Monte-Carlo dose recalculation, gamma vs TPS. Catches TPS dose errors & deliverability issues. | After Stage 1 |
| 3 | **Log** | Delivered dose reconstructed from the machine RT Ion Record, gamma vs TPS. Catches delivery/machine errors. | After a fraction is delivered |
| 4 | **Physical** | Result of a real measurement (pass/fail + optional gamma %), entered by a physicist. The training "ground truth". | When recorded by a human |

**The verdict** (three possible values):

- **`virtual_approve`** — High pass probability **and** high model confidence. A
  measurement may be skipped (only once the model is validated and you adopt this
  policy).
- **`flag`** — Elevated risk / borderline. Review recommended.
- **`measure`** — Default conservative outcome. Perform a physical measurement.

> In bootstrap mode (before the model is trained) **every** plan returns
> `measure`. That is intentional and correct.

---

## 3. System architecture

The application is a single Python web server that **also** serves the web UI:

```
+------------------------------------------------------------------+
|  Clinic workstation (Windows)                                    |
|                                                                  |
|   start.bat  ->  Python / Uvicorn  ->  FastAPI app (main.py)     |
|                                          |                       |
|          +-------------------------------+-----------------+     |
|          |                |               |                |     |
|     REST API         Folder watcher   Pipeline threads   Serves |
|     (/api/...)       (watchdog)        (Stage 1 / 2)      React  |
|          |                |               |              dist/   |
|          v                v               v                ^     |
|     SQLite DB       P:\PSQA_incoming   openMCsquare.exe     |     |
|     data\psqa.db    (TPS exports)      + BDL + scanners     |     |
+------------------------------------------------------------------+
                 ^
                 |  HTTP on port 8000 (LAN)
                 |
        Any clinic PC's web browser  ->  http://<server-name>:8000
```

**Key technology choices**

- **Backend framework:** FastAPI (Python 3.11+), served by Uvicorn.
- **Database:** SQLite (a single file, `data\psqa.db`) via SQLAlchemy ORM, with
  Alembic migrations.
- **Frontend:** React + TypeScript + Vite + Tailwind CSS, pre-built into static
  files in `frontend\dist\` and served by FastAPI.
- **Monte Carlo:** openMCsquare (external CPU executable you provide).
- **DICOM:** pydicom for RTPlan / RTDose / RTStruct / CT / RT Ion Record.
- **ML:** scikit-learn (logistic regression → random forest → gradient boosting,
  chosen automatically by data size).
- **Live updates:** Server-Sent Events (SSE) push worklist changes to the browser
  with no page refresh.

> Because FastAPI serves the built frontend, you only run **one** process. There
> is no separate Node server in production (Node is only needed once, to build the
> UI).

---

## 4. Project folder structure

```
virtual-psqa\
├── start.bat                 Launch the app.
├── setup.bat                 One-time install/build.
├── requirements.txt          Python dependencies.
├── DEPLOYMENT.md             Deployment notes (service install, firewall).
├── MANUAL.md                 This document.
│
├── backend\
│   ├── main.py               FastAPI entry point; folder watcher; SPA serving.
│   ├── config.py             ALL settings + their defaults (read this).
│   ├── .env                  YOUR overrides (you create this; not in git).
│   ├── .env.example          Template to copy to .env.
│   ├── database.py           SQLAlchemy engine/session.
│   ├── alembic\, alembic.ini Database migrations.
│   │
│   ├── models\               Database tables (ORM):
│   │     patient.py          Patient
│   │     plan.py             Plan (one proton plan)
│   │     fraction.py         Fraction (one delivered fraction / RT record)
│   │     qa_job.py           QAJob (a unit of background work + progress)
│   │     gamma_result.py     GammaResult (one gamma comparison result)
│   │     ml_prediction.py    MLPrediction (a scored verdict + recorded outcome)
│   │
│   ├── routers\              REST API endpoints (see §17):
│   │     patients, plans, jobs, results, reports, events,
│   │     ml, dashboard, settings_router
│   │
│   ├── services\             The engine room:
│   │     dicom_ingestor.py       Classify + import DICOM into DB + store.
│   │     folder_watcher.py       Watch P: drive for new exports.
│   │     pipeline.py             Stage 1 / Stage 2 orchestration.
│   │     complexity_extractor.py Layer 1 (plan complexity).
│   │     mcSquare_runner.py      Layer 2 (Monte Carlo build/run/parse).
│   │     log_reconstructor.py    Layer 3 (delivery-log dose reconstruction).
│   │     gamma_analysis.py       Orchestrates gamma comparisons.
│   │     gamma_engine.py         The actual gamma math.
│   │     dose_grid.py            3D dose grid container + geometry.
│   │     dose_cache.py           Caches computed dose grids on disk.
│   │     ml_predictor.py         Layer 4 fusion + train/predict.
│   │     job_runner.py           Executes a QAJob (one pipeline step).
│   │     job_control.py          Cooperative cancellation.
│   │     report_generator.py     Builds clinical reports.
│   │
│   ├── ml\
│   │     feature_schema.py   Canonical feature vector (order + defaults).
│   │     models\             Trained model files (created at runtime):
│   │                            model.joblib, model_meta.json, model_history.json
│   │
│   ├── dicom\
│   │     rtdose_parser.py    Find + load RTDose into a dose grid.
│   │
│   └── tests\                Pipeline / unit tests.
│
├── frontend\
│   ├── src\                  React source (pages, components, api, theme).
│   ├── dist\                 BUILT static UI that FastAPI serves (npm build).
│   └── package.json          Frontend dependencies + build scripts.
│
├── data\
│   ├── psqa.db               THE DATABASE (back this up).
│   ├── dicom_store\          Organised copy of every ingested DICOM file.
│   └── results\              Saved dose grids, gamma maps, thumbnails.
│
├── mcSquare\                 (You provide) openMCsquare install:
│   │   MCsquare_windows.exe  The executable.
│   │   BDL\                  Your commissioned Beam Data Library .txt.
│   │   Scanners\             HU->density / HU->material calibration.
│   │   Materials\            Material definitions shipped with MCsquare.
│
└── logs\                     Service/std logs (if installed as a service).
```

---

## 5. Prerequisites (what must be installed)

**On the workstation that will *run* the app:**

- Windows 10/11.
- **Python 3.11+** on PATH. (Verify: `python --version`.)
- **Node.js 18+** on PATH — **only needed once**, to build the web UI.
  (Verify: `node --version`.)
- **openMCsquare** (Windows build) + your commissioned BDL and scanner files, if
  you want **real** Monte-Carlo (Layer 2). Without it, the app runs in simulation
  mode and still works end to end.

**On the client machines that will *use* the app:** nothing — just a modern
browser (Chrome/Edge/Firefox).

**Network:**

- The server must be reachable on the clinic LAN on its port (default `8000`); a
  one-time Windows Firewall rule is required (see [§8](#8-starting-and-stopping-the-application)).
- For automatic ingestion, the server must have access to the shared folder the
  TPS exports into (e.g. a mapped `P:` drive or UNC path).

---

## 6. First-time installation

### Option A — Automated (recommended)

From the project root, double-click **`setup.bat`**. It will:

1. Create a Python virtual environment in `.venv\`.
2. Install all Python dependencies from `requirements.txt` into it.
3. Run `npm install` and `npm run build` to build the UI into `frontend\dist\`.
4. Copy `backend\.env.example` → `backend\.env` if you don't have one yet.

Then edit `backend\.env` ([§7](#7-configuration--backendenv-every-setting-explained))
and run `start.bat` ([§8](#8-starting-and-stopping-the-application)).

### Option B — Manual (equivalent steps)

```powershell
cd virtual-psqa
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
cd frontend
npm install
npm run build
cd ..
copy backend\.env.example backend\.env
```

**Notes**

- The database file is created automatically on first start. You can also run
  `alembic upgrade head` from `backend\` if you prefer migration-managed schema.
- You do **not** need to rebuild the frontend again unless the UI source changes.

---

## 7. Configuration — `backend\.env` (every setting explained)

All settings have safe defaults in `backend\config.py`. Override them with
`KEY=VALUE` lines in `backend\.env`. The app reads `.env` from the directory it's
started in — `start.bat` starts it in `backend\`, so `backend\.env` is used.

### Database & storage

| Key | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/psqa.db` | Where the database lives. Paths are relative to `backend\`. |
| `DICOM_STORE_PATH` | `./data/dicom_store` | Where ingested DICOM files are archived. |
| `RESULTS_PATH` | `./data/results` | Where dose grids, gamma maps, thumbnails are written. |

### Automatic ingestion (folder watcher)

| Key | Default | Meaning |
|---|---|---|
| `DICOM_WATCH_FOLDER` | *(unset)* | Shared folder the TPS exports into. When set (and it exists), new exports are auto-ingested. Leave unset to only upload manually. |
| `DICOM_WATCH_RECURSIVE` | `True` | Watch subfolders too (per-patient subfolders). |
| `DICOM_SETTLE_SECONDS` | `5` | Wait this long with no new files before treating a folder as finished copying. Increase for large/slow network copies. |

### Monte Carlo: simulation vs real

| Key | Default | Meaning |
|---|---|---|
| `MCSQUARE_SIMULATION_MODE` | `True` | `True` = synthesise a plausible MC dose from the TPS dose + noise (no binary needed). `False` = run the **real** openMCsquare binary (falls back to simulation + warning if the binary is missing). **Set `False` for clinical use once MCsquare is installed.** |
| `MCSQUARE_MOCK_NOISE` | `0.015` | 1-sigma fractional noise on the simulated dose (simulation mode only). |

### openMCsquare paths (used when `SIMULATION_MODE=False`)

| Key | Example | Meaning |
|---|---|---|
| `MCSQUARE_HOME` | `C:\PSQA\mcSquare` | Install dir containing the exe **and** `Materials\`, `Scanners\`, `BDL\`. MCsquare runs with this as its working directory. |
| `MCSQUARE_BINARY` | `C:\PSQA\mcSquare\MCsquare_windows.exe` | The executable. |
| `MCSQUARE_BDL_FILE` | `...\BDL\YourMachine_BDL.txt` | **Your commissioned** Beam Data Library (single `.txt`). Machine-specific; must match the planning machine. |
| `MCSQUARE_HU_DENSITY_FILE` | `...\Scanners\default\HU_Density_Conversion.txt` | HU → density calibration for your CT scanner (patient_ct geometry). |
| `MCSQUARE_HU_MATERIAL_FILE` | `...\Scanners\default\HU_Material_Conversion.txt` | HU → material calibration (patient_ct geometry). |

### Monte Carlo: geometry (important)

`MCSQUARE_GEOMETRY` controls what the secondary dose is computed **on**. It
**must** match what your ingested RTDose was computed on, or the gamma comparison
is invalid.

| Value | Use when… | Notes |
|---|---|---|
| `patient_ct` | Your RTDose is the normal patient-CT dose. | The patient CT series must be exported with the plan (one CT series per patient folder). MCsquare scores onto the RTDose grid so gamma stays index-aligned. |
| `water` | Your RTDose is a TPS **verification** dose computed on water (a phantom QA plan). | Recomputes on a water box matched to the RTDose grid. |
| `auto` *(default)* | — | Uses `patient_ct` if a CT series is present, else `water`. |

> If your clinic exports patient-CT RTDose with one CT per patient folder, set
> `MCSQUARE_GEOMETRY=patient_ct` (or leave `auto`).

### Monte Carlo: statistics

| Key | Default | Meaning |
|---|---|---|
| `MCSQUARE_PRIMARIES` | `1000000` (`.env.example` uses `10000000`) | Primary protons to simulate. More = smoother but slower. |
| `MCSQUARE_NUM_THREADS` | `0` | CPU threads. `0` = all cores. CPU-only (no GPU). |
| `MCSQUARE_STAT_UNCERTAINTY` | `2.0` | Target statistical uncertainty (%). `0` = run exactly `MCSQUARE_PRIMARIES`. |
| `MCSQUARE_DOSE_TO_WATER` | `Disabled` | `Disabled` \| `PostProcessing` \| `OnlineSPR`. On water, dose-to-medium == dose-to-water. For `patient_ct` the app auto-upgrades to `PostProcessing` to match a TPS reporting dose-to-water. |

### Phantom (water geometry only)

| Key | Default | Meaning |
|---|---|---|
| `PHANTOM_LATERAL_SIZE_MM` | `300.0` | Water box lateral size. |
| `PHANTOM_VOXEL_SIZE_MM` | `2.0` | Water box voxel size. |

### Log reconstruction (Layer 3)

| Key | Default | Meaning |
|---|---|---|
| `LOG_RECON_SPOT_SIGMA_MM` | `5.0` | Lateral Gaussian sigma (mm) used to paint each delivered spot. |

### Gamma criteria (see [§14](#14-gamma-analysis-how-doses-are-compared))

| Key | Default | Meaning |
|---|---|---|
| `GAMMA_MCSQUARE_VS_TPS_DD` | `1.75` | % dose-difference (MCsquare vs TPS) |
| `GAMMA_MCSQUARE_VS_TPS_DTA` | `2.0` | mm distance-to-agreement |
| `GAMMA_MCSQUARE_VS_TPS_THRESHOLD` | `95.0` | % passing-rate needed to pass |
| `GAMMA_LOG_VS_TPS_DD` | `3.0` | % (log vs TPS) |
| `GAMMA_LOG_VS_TPS_DTA` | `2.0` | mm |
| `GAMMA_LOG_VS_TPS_THRESHOLD` | `90.0` | % |
| `GAMMA_CONCORDANCE_DD` | `2.0` | % (MCsquare vs log concordance) |
| `GAMMA_CONCORDANCE_DTA` | `2.0` | mm |
| `GAMMA_CONCORDANCE_THRESHOLD` | `90.0` | % |
| `DOSE_THRESHOLD_PERCENT` | `10.0` | Low-dose cutoff: voxels below this % of max are excluded from gamma. |

### Machine learning (see [§15](#15-the-machine-learning-prediction-engine))

| Key | Default | Meaning |
|---|---|---|
| `ML_MODEL_DIR` | `./ml/models` | Where trained model files are stored. |
| `ML_APPROVE_PROBABILITY` | `0.92` | ≥ this **and** high confidence → `virtual_approve`. |
| `ML_FLAG_PROBABILITY` | `0.80` | ≥ this → `flag`; below → `measure`. |
| `ML_RETRAIN_MIN_CASES` | `50` | Recorded outcomes required before training. |
| `COMPLEXITY_SAS_THRESHOLD` | `0.005` | Spot-weight cutoff for the SAS metric. |

### Pipeline

| Key | Default | Meaning |
|---|---|---|
| `PIPELINE_AUTO_RUN` | `True` | Run the full Stage-1 chain automatically on ingest (and Stage-2 on a delivery log). Set `False` to trigger steps manually. |

### Orthanc PACS / VNA Server Integration

| Key | Default | Meaning |
|---|---|---|
| `ORTHANC_URL` | `http://localhost:8042` | URL of the clinical Orthanc PACS server. |
| `ORTHANC_USERNAME` | *(unset)* | HTTP Basic Auth username for Orthanc (if authentication enabled). |
| `ORTHANC_PASSWORD` | *(unset)* | HTTP Basic Auth password for Orthanc (if authentication enabled). |
| `ORTHANC_TIMEOUT_SECONDS` | `120` | HTTP request timeout for large volume downloads (e.g. multi-slice CT/CBCT zip archives). |

> Anything you do **not** put in `.env` keeps the default from `config.py`.

---

## 8. Starting and stopping the application

**Day-to-day start (foreground).** Double-click **`start.bat`** (or run it from a
terminal).

- Uses `.venv` if present, else system Python.
- Serves on all interfaces, port `8000` by default.
- Local URL: `http://localhost:8000`
- LAN URL: `http://<this-computer-name>:8000` (or `http://<IP>:8000`)
- Press **Ctrl+C** in the window to stop.

**Change the port:**

```powershell
set PSQA_PORT=8080
start.bat
```

**Find your LAN IP (to share with colleagues):**

```powershell
ipconfig   # look for IPv4 Address, e.g. 192.168.1.105
```

**Open the firewall (one time, run in an *admin* PowerShell):**

```powershell
New-NetFirewallRule -DisplayName "Virtual PSQA" -Direction Inbound `
  -Protocol TCP -LocalPort 8000 -Action Allow
```

**Run as an always-on Windows service (survives reboot/logout).** Use NSSM
(<https://nssm.cc>). Full steps are in `DEPLOYMENT.md` §7. In short: install a
service that runs `python backend\main.py` with `AppDirectory` set to `backend\`.
The service auto-serves the UI and the watcher.

---

## 9. How data flows in: DICOM types and the two-stage pipeline

**DICOM the app understands**

| Modality | Prefix | Role |
|---|---|---|
| RTPLAN / RTIONPLAN | `RP` | The proton plan (beams, spots, MU, fractions). |
| RTDOSE | `RD` | The TPS-calculated dose (the reference). |
| RTSTRUCT | `RS` | Structures (optional; context). |
| CT series | `CT` | Patient planning CT (needed for `patient_ct` MC). |
| RT ION RECORD | `RI` | Machine delivery log for a fraction (Stage 2). |

**Ingestion — three pathways**

- **Orthanc PACS / VNA Integration (Recommended):** Connect directly to your hospital Orthanc PACS server. From the Dashboard or Plan detail view, click **Import from Orthanc** to search patients by ID or name. You can:
  1. Inspect available studies, planning CTs, RT plans, RT doses, and RT structures.
  2. Ingest selected plans (with associated Planning CT) directly into Virtual-PSQA, triggering Stage 1 Monte Carlo & secondary QA calculation.
  3. Import delivered RT Ion Records on demand to trigger Stage 2 Delivery QA.
  4. Import daily Offline Image Reviews (CBCT & Spatial Registration / REG objects) directly into the Offline Image Review (OIR) and Synthetic CT modules for target fraction anatomical verification.
- **Automatic Watch Folder:** set `DICOM_WATCH_FOLDER`. The watcher notices a new/changed subfolder, waits `DICOM_SETTLE_SECONDS` for copying to finish, then ingests.
- **Manual Upload:** upload through the UI (Plan Ingestion page) or `POST /api/plans/upload`.

**Recommended export layout (one folder per patient plan):**

```
P:\PSQA_incoming\
  PT_2024_0347_HN\
     RP.<uid>.dcm        (RTIonPlan)
     RD.<uid>.dcm        (RTDose)
     RS.<uid>.dcm        (RTStruct, optional)
     CT.<uid>.*.dcm      (CT series — one series for this patient)
     RI.<uid>.dcm        (RT Ion Record — arrives later, after delivery)
```

**Stage 1 — Plan arrival** (triggered by an RTPlan in the batch):

1. Ingest plan + dose + struct into DB and `dicom_store`.
2. Initial ML prediction from complexity alone (conservative bootstrap).
3. Run MCsquare secondary dose (real or simulated).
4. Gamma: MCsquare vs TPS.
5. Re-score ML prediction now that MC evidence exists.
6. Push a live update to the dashboard (SSE).

**Stage 2 — Fraction delivery** (triggered by an RT Ion Record alone):

1. Match the record to its plan (by referenced plan UID/identifiers).
2. Reconstruct the delivered dose from the log.
3. Gamma: reconstructed log dose vs TPS, tagged with the fraction number.
4. Re-score ML prediction (log evidence now available).
5. Push a live update.

Each step is wrapped in a **QAJob** so it appears in the activity feed with
progress and can be cancelled. Stages run on background threads so ingestion never
blocks. If `PIPELINE_AUTO_RUN=False`, you trigger steps manually instead.

---

## 10. Evidence layer 1 — Plan complexity

Computed directly from the RTIonPlan; no simulation needed (always available).
Implemented in `services\complexity_extractor.py`. Key metrics:

- **MCS** — Modulation Complexity Score (Li et al. 2013, adapted for PBS). Higher
  modulation generally means higher deliverability risk.
- **SAS** — Small Aperture Score: fraction of spots whose weight is below
  `COMPLEXITY_SAS_THRESHOLD` of the max layer weight. Many tiny spots are harder to
  deliver accurately.
- **MU/Gy** — total monitor units per prescribed Gy (efficiency/modulation).
- Aggregates: `n_fields`, `n_fractions`, `total_spots`, `total_layers`,
  `total_mu`, mean/max spots-per-layer, mean energy range, mean MU per spot, mean
  field size.

These become features in the ML vector ([§15](#15-the-machine-learning-prediction-engine))
and are shown on the plan's **Complexity** evidence card with each feature's
contribution.

---

## 11. Evidence layer 2 — MCsquare secondary Monte-Carlo dose

Implemented in `services\mcSquare_runner.py`. This is the **independent** dose check.

**Real mode (`SIMULATION_MODE=False`)**

1. Builds an MCsquare run folder containing:
   - `config.txt` generated from your `.env` settings + the plan.
   - the plan beams/spots translated for MCsquare.
   - geometry: either a water box (`water`) or the patient CT written as a
     MetaImage (`CT.mhd`/`CT.raw`) loaded from the exported CT series (`patient_ct`).
   - an independent scoring grid matched to the TPS RTDose grid so results are
     index-aligned for gamma.
2. Runs `MCsquare.exe` with `MCSQUARE_HOME` as the working directory (so it can find
   `Materials\`). Uses `MCSQUARE_PRIMARIES` / `NUM_THREADS` / `STAT_UNCERTAINTY`.
3. Reads the output `Dose.mhd`/`Dose.raw` back into a dose grid.
4. On failure, the error message includes the last ~40 lines of MCsquare
   stdout/stderr to diagnose commissioning/path problems.

**Simulation mode (`SIMULATION_MODE=True`, the default)** — instead of shelling
out, it derives a physically-plausible MC dose from the TPS dose plus Gaussian
noise (`MCSQUARE_MOCK_NOISE`). The pipeline, gamma, UI, and ML all behave the same;
only the dose source differs. Use this until the real binary is commissioned.

**Requirements for real mode**

- openMCsquare Windows executable.
- `Materials\`, `Scanners\` (with your HU calibration), `BDL\` (your commissioned
  machine file) inside `MCSQUARE_HOME`.
- For `patient_ct` geometry: the patient CT series exported with the plan.
- `MCSQUARE_GEOMETRY` set to match what your RTDose was computed on.

---

## 12. Evidence layer 3 — Delivery log (RT Ion Record) reconstruction

Implemented in `services\log_reconstructor.py`. Available only after treatment.

1. Parses the RT Ion Beam Treatment Record (`RI…`) for the **delivered** spot
   positions and metersets. Prefers `ScanSpotMetersetsDelivered`, falling back to
   `ScanSpotMetersetWeights` if the delivered tag is absent.
2. Reconstructs a 3D dose on the TPS RTDose grid with a simplified pencil-beam
   model: each spot is a lateral 2D Gaussian (sigma = `LOG_RECON_SPOT_SIGMA_MM`)
   placed in depth at the proton range for its nominal energy (Bortfeld
   range-energy relation) with a Bragg-peak-like longitudinal spread.
3. The result is gamma-compared to the TPS dose (`log_vs_TPS`), per fraction.

This catches **delivery** errors (the machine didn't deliver what was planned).
It is a Phase-2 simplification: full physics (scatter, nuclear halo, tissue
heterogeneity) is intentionally out of scope for this layer.

---

## 13. Evidence layer 4 — Physical measurement (recorded outcome)

This is the human-entered **ground truth**. On a plan's detail page use **Record
physical QA outcome** to enter:

- pass / fail
- (optional) the measured gamma passing rate (%)

Each recorded outcome:

- Becomes a labelled training example for the ML model.
- Is shown on the plan's **Physical measurement** evidence card.

The model can only learn from plans that have a recorded outcome. Recording
outcomes diligently during the data-collection phase is what eventually enables
trustworthy `virtual_approve` verdicts.

---

## 14. Gamma analysis (how doses are compared)

Implemented in `services\gamma_analysis.py` (orchestration) and
`services\gamma_engine.py` (the math). The gamma index combines a dose-difference
criterion (DD, %) and a distance-to-agreement criterion (DTA, mm). A voxel
**passes** if its gamma ≤ 1. The **passing rate** is the % of evaluated voxels that
pass; a comparison **passes** if its passing rate ≥ its threshold.

**Three comparisons** (defaults from the build plan; tune in `.env`):

| Comparison | Criteria | Pass threshold | Purpose |
|---|---|---|---|
| `mcSquare_vs_TPS` | 1.75% / 2 mm | ≥ 95% | Independent MC check |
| `log_vs_TPS` | 3.0% / 2 mm | ≥ 90% | Delivery check (TG-218-like) |
| `mcSquare_vs_log` | 2.0% / 2 mm | ≥ 90% | Concordance of the two checks |

Voxels below `DOSE_THRESHOLD_PERCENT` of the reference max dose are excluded (the
standard low-dose cutoff). Comparisons run on the reference's max-dose plane;
gamma maps/thumbnails are saved under `data\results\` for display and reports.

> **Coordinate alignment.** For valid gamma, the MC/log doses are computed/scored
> onto the **same grid** as the TPS RTDose. This is why `MCSQUARE_GEOMETRY` must
> match how the RTDose was produced ([§7](#7-configuration--backendenv-every-setting-explained)).

---

## 15. The machine-learning prediction engine

Implemented in `services\ml_predictor.py` with the feature schema in
`ml\feature_schema.py`. It fuses all four layers into one pass probability + verdict.

**The feature vector** (fixed order; new features appended at the end):

- **Complexity:** `n_fields`, `n_fractions`, `total_spots`, `total_layers`,
  `total_mu`, `mcs`, `sas`, `mu_gy`, `mean_spots_per_layer`, `max_spots_per_layer`,
  `mean_energy_range_mev`, `mean_mu_per_spot`, `mean_field_size_cm2`
- **MCsquare:** `mcsquare_mean_pr`, `mcsquare_min_pr`, `mcsquare_all_pass`
- **Log:** `log_mean_pr`, `log_min_pr`, `log_all_pass`, `log_n_fractions`
- **Physical:** `physical_passing_rate`, `physical_measured`

When a layer's evidence is missing, neutral defaults are used, and the UI shows
which layers are present vs missing (*evidence completeness*).

**Bootstrap mode** (no trained model yet):

- Computes a transparent **heuristic** probability (from available gamma passing
  rates, penalised by high SAS / low MCS) **for display only**.
- Always returns `verdict = measure`, `confidence = low`.
- Correct, conservative cold-start: trust nothing until trained.

**Training:**

- Needs at least `ML_RETRAIN_MIN_CASES` (default 50) plans **with** recorded
  physical outcomes.
- Trigger from the UI (Model Insights → **Retrain now**) or `POST /api/ml/retrain`.
- Algorithm chosen automatically by sample count:

  | Cases | Algorithm |
  |---|---|
  | < 200 | logistic regression |
  | < 500 | random forest |
  | ≥ 500 | gradient boosting |

- Honest metrics via stratified 5-fold cross-validation: AUC, accuracy,
  sensitivity, specificity, PPV, NPV, confusion matrix. The final model is then fit
  on all data and saved.

**Confidence** (how much to trust a prediction):

| Level | Condition |
|---|---|
| `high` | n ≥ 200 **and** AUC ≥ 0.80 **and** decision margin ≥ 0.30 **and** ≥ 2 evidence layers |
| `moderate` | n ≥ `ML_RETRAIN_MIN_CASES` **and** AUC ≥ 0.70 **and** ≥ 1 evidence layer |
| `low` | otherwise (always, in bootstrap mode) |

**Verdict rule:**

- `virtual_approve` — prob ≥ `ML_APPROVE_PROBABILITY` (0.92) **and** confidence == `high`
- `flag` — prob ≥ `ML_FLAG_PROBABILITY` (0.80)
- `measure` — otherwise

**Model files** (in `backend\ml\models\`):

| File | Contents |
|---|---|
| `model.joblib` | The trained scikit-learn pipeline (scaler + classifier). |
| `model_meta.json` | Current model metadata (version, metrics, importances). |
| `model_history.json` | Append-only log of every training run. |

**Reset the model** (back to bootstrap): stop the app and delete the files in
`backend\ml\models\`.

---

## 16. Using the web interface (page by page)

Open `http://<server>:8000` in a browser. The top navigation bar shows the app
title/breadcrumb, links, a live clock, and a pulsing "live" dot (SSE connected).

- **Dashboard** — KPIs, a grouped action worklist (measure → flag → divider →
  approved), evidence-coverage summary, and a live pipeline activity feed. Updates
  in real time.
- **Worklist / Patient Plans** — browse patients and their plans; open a plan.
- **Plan Detail** (the main screen):
  - Breadcrumb + verdict badge.
  - Header card: plan metadata + an auto-generated alert summarising risk.
  - Verdict card: an arc gauge of pass probability + confidence.
  - 2×2 evidence grid: Complexity (with feature contributions), MCsquare (gamma vs
    TPS), Log reconstruction (gamma vs TPS per fraction), Physical measurement
    (with **Record outcome** action).
  - Action footer (approve / flag / measure / report).
- **Plan Ingestion** — manually upload a DICOM folder/files instead of the watcher.
- **Simulation Runner** — manually trigger pipeline steps (MCsquare, gamma).
- **Gamma Detail / Dose Comparison** — inspect gamma maps and compare dose planes.
- **Fractional Tracker** — per-fraction trend of delivery gamma passing rate.
- **Model Insights** — model status, metrics, feature importances, history, and
  the **Retrain now** button.
- **Settings** — view effective configuration.

---

## 17. The REST API (endpoint reference)

All endpoints are also documented interactively at `http://<server>:8000/docs`
(FastAPI Swagger UI).

**Patients**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/patients` | list patients + latest plan |
| GET | `/api/patients/{patient_id}/plans` | plans for a patient |

**Plans**

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/plans/upload` | upload + ingest DICOM |
| GET | `/api/plans/{plan_id}` | plan summary |
| GET | `/api/plans/{plan_id}/fields` | per-field summary |

**Jobs (background work)**

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/jobs` | create a job |
| POST | `/api/jobs/run` | create + run a job |
| POST | `/api/jobs/{job_id}/cancel` | cancel a running job |
| GET | `/api/jobs/{job_id}` | job status/progress |
| GET | `/api/jobs/plan/{plan_id}` | jobs for a plan |

**Results (doses & gamma)**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/results/plan/{plan_id}` | gamma results for a plan |
| GET | `/api/results/plan/{plan_id}/doses` | available dose sources |
| GET | `/api/results/plan/{plan_id}/dose/{source}/plane/{z}` | a dose plane |
| GET | `/api/results/plan/{plan_id}/gamma/{comparison}/plane/{z}` | a gamma plane |

**ML**

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/ml/plans/{plan_id}/predict` | score a plan now |
| GET | `/api/ml/plans/{plan_id}/predictions` | all predictions for a plan |
| GET | `/api/ml/plans/{plan_id}/prediction` | latest prediction |
| POST | `/api/ml/plans/{plan_id}/outcome` | record physical outcome (label) |
| POST | `/api/ml/retrain` | retrain the model |
| GET | `/api/ml/performance` | model status + metrics |
| GET | `/api/ml/history` | training history |

**Dashboard / Reports / Settings / Events**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/dashboard` | dashboard aggregate |
| GET | `/api/reports/{plan_id}` | clinical report (`html`\|`pdf`) |
| GET | `/api/reports/{plan_id}/fractional-trend` | per-fraction trend data |
| GET | `/api/settings` | effective settings |
| GET | `/api/events/worklist` | SSE stream of live updates |

Exact request/response shapes are in `backend\routers\` and `backend\schemas\`,
and visible in `/docs`.

---

## 18. The database, backups, and resets

The database is a single SQLite file: `data\psqa.db`. Tables (`backend\models\`):
Patient, Plan, Fraction, QAJob, GammaResult, MLPrediction.

**Back up** (do this regularly): with the app stopped, copy `data\psqa.db` and
(optionally, to preserve doses/maps and the trained model) `data\results\` and
`backend\ml\models\`.

**Reset the database** (wipe **all** plans/predictions). Stop the app, then from
`backend\` run:

```powershell
.venv\Scripts\python -c "import models; from database import Base, engine; Base.metadata.drop_all(bind=engine); Base.metadata.create_all(bind=engine)"
```

**Reset only the model** (keep data, return to bootstrap): stop the app and delete
the files in `backend\ml\models\`.

**Schema migrations:** managed by Alembic. After updating code that changes the
schema, run from `backend\`: `alembic upgrade head`.

---

## 19. Reports

Open a plan → **Report**, or `GET /api/reports/{plan_id}` for a print-ready
clinical report. Use the browser's **Save as PDF** for an archival copy.

True server-side PDF (`GET /api/reports/{plan_id}?format=pdf`) requires WeasyPrint
plus the GTK runtime (Pango/Cairo). On Windows that GTK install is fiddly, so if it
is not present the endpoint gracefully returns the **same report as HTML** — no GTK
required. Browser "Save as PDF" is the simplest archival path on Windows.

---

## 20. Updating the application

1. Replace/pull the new code.
2. Rebuild the UI if the frontend changed: `cd frontend && npm run build && cd ..`
3. Apply DB migrations if any: `cd backend && alembic upgrade head && cd ..`
4. Restart: close the `start.bat` window and run it again (or
   `Restart-Service VirtualPSQA` if installed as a service).

Re-installing Python deps is only needed if `requirements.txt` changed:

```powershell
.venv\Scripts\python -m pip install -r requirements.txt
```

---

## 21. Troubleshooting

| Symptom | Check / fix |
|---|---|
| App won't start / "python not found" | Install Python 3.11+ on PATH, or run `setup.bat` to create the `.venv` that `start.bat` prefers. |
| `UnicodeEncodeError` in the console | `start.bat` sets `PYTHONIOENCODING=utf-8`; if starting manually, set that env var too. |
| Not reachable from other machines | Run the firewall rule ([§8](#8-starting-and-stopping-the-application)); use the server's LAN IP/name and correct port; confirm same network. |
| DICOM files not auto-ingested | Confirm `DICOM_WATCH_FOLDER` is set, exists, and is accessible from the server account. Increase `DICOM_SETTLE_SECONDS`. Look for "Folder watcher active on:" in the console. |
| Frontend not loading / blank page | Rebuild: `cd frontend && npm run build`. Server logs "Serving frontend from: …\frontend\dist"; if "dist not found", build it. |
| MCsquare error / "exited with code N" | Error includes the last lines of MCsquare output. Common causes: wrong `MCSQUARE_HOME` (can't find `Materials\`), wrong BDL, geometry/RTDose mismatch, missing CT for `patient_ct`. Or set `SIMULATION_MODE=True` while fixing commissioning. |
| Gamma looks wrong / fails | Almost always a geometry mismatch: `MCSQUARE_GEOMETRY` must match how the RTDose was computed (`patient_ct` vs `water`). |
| Every verdict is "measure" | Expected in bootstrap mode (until ≥ `ML_RETRAIN_MIN_CASES` outcomes recorded **and** you retrain). Correct, safe behaviour. |
| PDF report returns HTML | WeasyPrint/GTK not installed — use browser Save as PDF. |
| Database errors after update | Run `alembic upgrade head` from `backend\`. |
| Service won't start (NSSM) | Check `logs\service_error.log`. |

---

## 22. Clinical safety — read before relying on verdicts

This application is decision **support** and a data-collection platform. Treat its
output as advisory until you have locally validated it.

- **Default is safe.** In bootstrap mode every plan returns `measure`. Do **not**
  skip physical QA based on this tool until you have trained **and** validated a
  model on your **own** clinic's recorded outcomes and your department has approved
  a policy.
- **Simulation mode.** Out of the box `MCSQUARE_SIMULATION_MODE=True`, so the
  "Monte Carlo" dose is **synthetic**, not a real independent calculation. For any
  real verification you must install/commission openMCsquare and set it `False`.
- **Geometry matters.** An `MCSQUARE_GEOMETRY` that doesn't match how your RTDose
  was computed makes the gamma comparison meaningless. Verify this first.
- **Commissioning.** BDL and HU calibration files are machine/scanner specific.
  Garbage in → garbage out. Use your commissioned files.
- **Log reconstruction** is a simplified pencil-beam model (no full
  heterogeneity/scatter physics). Interpret `log_vs_TPS` gamma accordingly.
- **Security.** The server listens on the LAN with permissive CORS and **no
  built-in authentication**. Deploy only on a trusted, firewalled clinical subnet.
  Do not expose it to the internet. Add authentication/network controls before any
  broad rollout, per your institution's policy.
- **Validation & governance.** Model performance (AUC/sensitivity/specificity)
  must meet your acceptance criteria, and the decision to act on `virtual_approve`
  is a clinical / medical-physics governance decision, not a software default.

**Recommended adoption path**

1. Run in **shadow mode**: collect plans + record **every** physical QA outcome.
2. Once ≥ 50 (ideally many more) outcomes exist, retrain and review metrics.
3. **Validate prospectively** before changing any clinical workflow.

---

## 23. Quick-start checklist

- [ ] Install Python 3.11+ and Node.js 18+ (Node only needed for the build).
- [ ] Run `setup.bat` (creates `.venv`, installs deps, builds UI, makes `.env`).
- [ ] Edit `backend\.env`:
  - [ ] `DICOM_WATCH_FOLDER` → your TPS export share (or leave unset).
  - [ ] For real MC: `MCSQUARE_SIMULATION_MODE=False` and set `MCSQUARE_HOME`,
    `MCSQUARE_BINARY`, `MCSQUARE_BDL_FILE`, HU calibration files.
  - [ ] `MCSQUARE_GEOMETRY=patient_ct` (if RTDose is patient-CT dose) or `water`.
- [ ] Open the firewall on the chosen port (one-time, admin).
- [ ] Run `start.bat`.
- [ ] Browse to `http://<server>:8000` from a clinic PC.
- [ ] Ingest a test plan; watch Stage 1 run on the Dashboard.
- [ ] Record physical outcomes for every plan during data collection.
- [ ] Retrain once you have ≥ 50 outcomes; review metrics in Model Insights.
- [ ] **Do not** change clinical workflow until locally validated and approved.

---

*End of manual.*
