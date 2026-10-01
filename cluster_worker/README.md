# Virtual PSQA - Distributed Monte Carlo Worker Node

This package allows idle computers on the hospital / clinic network to assist with secondary Monte Carlo dose simulations (`openMCsquare`).

## Overview
- **Zero Impact on Users:** The worker monitors mouse and keyboard activity (`GetLastInputInfo` on Windows). It only accepts jobs when the machine has been idle for $> 5$ minutes and background CPU is $< 30\%$. If a user moves the mouse or types, it immediately yields the CPU.
- **Hospital Firewall-Proof (PULL Mode):** In hospital networks where the server cannot reach workstation subnets across VLANs, the worker reaches **outbound** to the server. Zero incoming firewall rules are required on the workstation.
- **Prioritization:** Simulations run at low OS scheduling priority (`BELOW_NORMAL_PRIORITY_CLASS` on Windows / `nice 10` on Linux) so background tasks never cause system stutter.
- **Dynamic Failover:** If a user returns to their desk or the computer shuts down, the primary Virtual-PSQA server automatically catches the interruption and re-runs the beam locally or on another idle machine.

## Setup on a Network PC

### Prerequisites
1. Python 3.10+ installed on the worker PC.
2. The `MCsquare` folder containing the executable and `BDL/`, `Materials/`, `Scanners/`.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

### Running the Worker (Recommended: PULL Mode)

Simply double-click `run_worker.bat` on Windows (or `./run_worker.sh` on Linux):
1. When prompted, enter your Virtual PSQA Server URL (e.g. `http://172.20.145.65:8003` or whatever address you type in your browser).
2. The worker will automatically save it in `cluster_worker/server_url.txt` and begin checking in with the server.
3. In the Virtual PSQA web app under **Settings → Distributed MCsquare Compute Cluster**, your workstation will automatically appear with its live status, CPU cores, and idle state!

### Command Line Options:
```cmd
REM Launch with specific server URL and dedicated compute mode (0 idle minutes):
run_worker.bat http://172.20.145.65:8003 0

REM Reset saved server URL configuration:
run_worker.bat reset

REM Python direct usage:
python vpsqa_worker.py --server-url http://172.20.145.65:8003 --mcsquare-dir C:\Path\To\MCsquare
```
