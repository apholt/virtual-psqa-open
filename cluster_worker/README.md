# Virtual PSQA - Distributed Monte Carlo Worker Node

This package allows idle computers on the hospital / clinic network to assist with secondary Monte Carlo dose simulations (`openMCsquare`).

## Overview
- **Zero Impact on Users:** The worker monitors mouse and keyboard activity (`GetLastInputInfo` on Windows). It only accepts jobs when the machine has been idle for $> 5$ minutes and background CPU is $< 30\%$.
- **Prioritization:** Simulations run at low OS scheduling priority (`BELOW_NORMAL_PRIORITY_CLASS` on Windows / `nice 10` on Linux) so background tasks never cause system stutter.
- **Dynamic Failover:** If a user returns to their desk or the computer shuts down, the primary Virtual-PSQA server automatically catches the interruption and re-runs the beam locally or on another idle machine.

## Setup on a Network PC

### Prerequisites
1. Python 3.10+ installed on the worker PC.
2. The `MCsquare` folder containing the executable and `BDL/`, `Materials/`, `Scanners/`.
3. Install dependencies:
   ```bash
   pip install fastapi uvicorn numpy httpx
   ```

### Running the Worker
- **Windows:** Double-click `run_worker.bat` (or add a shortcut to the Windows Startup folder).
- **Command Line:**
  ```bash
  python vpsqa_worker.py --port 8001 --mcsquare-dir C:\Path\To\MCsquare
  ```

### Adding this PC to the Virtual-PSQA Server
In the Virtual-PSQA web interface:
1. Navigate to **Settings &rarr; Compute Cluster**.
2. Add the worker node's IP or hostname (e.g. `http://192.168.1.105:8001`).
3. Click **Test & Connect**. Once connected, multi-beam plans will automatically distribute their calculations across this computer!
