from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


import subprocess


def _is_binary_compatible(bin_path: Path) -> bool:
    """Test if binary exists and passes host CPU instruction verification."""
    if not bin_path.is_file() or not bin_path.exists():
        return False
    if platform.system().lower() != "windows":
        try:
            bin_path.chmod(bin_path.stat().st_mode | 0o755)
        except Exception:
            pass
    try:
        proc = subprocess.run([str(bin_path)], capture_output=True, text=True, timeout=1)
        out = (proc.stdout or "") + (proc.stderr or "")
        if "Please verify that both the operating system and the processor" in out:
            return False
        return True
    except Exception:
        return False


def _resolve_default_mcsquare_home() -> str:
    """Find MCsquare directory cross-platform (handling case-sensitivity)."""
    cur = Path.cwd()
    for parent in [cur, cur.parent]:
        for name in ["MCsquare", "mcSquare", "mcsquare"]:
            candidate = parent / name
            if candidate.is_dir() and (candidate / "BDL").is_dir():
                return str(candidate)
    return "./MCsquare"


def _resolve_default_mcsquare_exe() -> str:
    """Detect OS and CPU instruction set compatible default MCsquare binary name."""
    is_win = platform.system().lower() == "windows"
    if is_win:
        return "MCsquare_win_avx2.exe"

    home = Path(_resolve_default_mcsquare_home())
    candidates = ["MCsquare_linux_avx2", "MCsquare_linux", "MCsquare_linux_avx", "MCsquare_linux_sse4", "MCsquare_linux_avx512"]
    for c in candidates:
        candidate_path = home / c
        if candidate_path.exists() and _is_binary_compatible(candidate_path):
            return c
    return "MCsquare_linux"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("PSQA_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    DATABASE_URL: str = "sqlite:///./data/psqa.db"

    # Authentication & Security (HIPAA Safeguards)
    AUTH_ENABLED: bool = True
    AUTH_USERNAME: str = "admin"
    AUTH_PASSWORD: str = "psqa-admin-2026!"
    AUTH_USERS: Optional[str] = None  # Format: "user1:pass1,user2:pass2" or JSON
    AUTH_SECRET_KEY: str = "vpsqa-secure-default-change-me-in-production-193850123"
    AUTH_SESSION_COOKIE: str = "vpsqa_session"
    AUTH_SESSION_EXPIRE_MINUTES: int = 15  # HIPAA § 164.312(a)(2)(iii): 15-min idle logoff
    
    # Transmission Security (HIPAA § 164.312(e): TLS/HTTPS)
    SSL_ENABLED: bool = True
    SSL_KEYFILE: Optional[str] = "./certs/key.pem"
    SSL_CERTFILE: Optional[str] = "./certs/cert.pem"

    # Storage paths
    DICOM_STORE_PATH: str = "./data/dicom_store"
    RESULTS_PATH: str = "./data/results"

    # Folder watcher
    DICOM_WATCH_FOLDER: Optional[str] = None  # e.g. r"P:\PSQA_incoming" or "/path/to/watch"
    DICOM_WATCH_RECURSIVE: bool = True
    DICOM_SETTLE_SECONDS: int = 5

    # Orthanc PACS / VNA server integration
    ORTHANC_URL: str = "http://localhost:8042"
    ORTHANC_USERNAME: Optional[str] = None
    ORTHANC_PASSWORD: Optional[str] = None
    ORTHANC_TIMEOUT_SECONDS: int = 120

    # ---- MCsquare (openMCsquare CPU Monte Carlo) ----
    # Install directory containing executable + Materials/, Scanners/, BDL/
    MCSQUARE_HOME: str = _resolve_default_mcsquare_home()
    # Path / filename of the openMCsquare executable
    MCSQUARE_BINARY: str = f"{_resolve_default_mcsquare_home()}/{_resolve_default_mcsquare_exe()}"
    MCSQUARE_EXE: str = _resolve_default_mcsquare_exe()
    MCSQUARE_BDL_NAME: str = "auto"
    MCSQUARE_SCANNER: str = "default"
    # Commissioned Beam Data Library file
    MCSQUARE_BDL_FILE: str = "./MCsquare/BDL/BDL.txt"
    # Legacy folder form of BDL path
    MCSQUARE_BDL_PATH: str = "./MCsquare/BDL"
    # HU -> density / material calibrations
    MCSQUARE_HU_DENSITY_FILE: str = "./MCsquare/Scanners/default/HU_Density_Conversion.txt"
    MCSQUARE_HU_MATERIAL_FILE: str = "./MCsquare/Scanners/default/HU_Material_Conversion.txt"
    MCSQUARE_PRIMARIES: int = 1_000_000
    MCSQUARE_NUM_THREADS: int = 0
    MCSQUARE_STAT_UNCERTAINTY: float = 1.5
    MCSQUARE_DOSE_TO_WATER: str = "Disabled"
    MCSQUARE_GEOMETRY: str = "auto"
    MCSQUARE_GPU_ID: int = 0
    PROTON_RBE: float = 1.10

    # Simulation mode (mock MC dose if binary missing or during dev)
    MCSQUARE_SIMULATION_MODE: bool = False
    MCSQUARE_MOCK_NOISE: float = 0.015

    # Phantom geometry (water phantom mode)
    PHANTOM_LATERAL_SIZE_MM: float = 300.0
    PHANTOM_VOXEL_SIZE_MM: float = 2.0

    # Log reconstruction
    LOG_RECON_SPOT_SIGMA_MM: float = 5.0

    # Gamma thresholds
    GAMMA_MCSQUARE_VS_TPS_DD: float = 3.0
    GAMMA_MCSQUARE_VS_TPS_DTA: float = 3.0
    GAMMA_MCSQUARE_VS_TPS_THRESHOLD: float = 90.0

    GAMMA_LOG_VS_TPS_DD: float = 3.0
    GAMMA_LOG_VS_TPS_DTA: float = 2.0
    GAMMA_LOG_VS_TPS_THRESHOLD: float = 90.0

    GAMMA_CONCORDANCE_DD: float = 2.0
    GAMMA_CONCORDANCE_DTA: float = 2.0
    GAMMA_CONCORDANCE_THRESHOLD: float = 90.0

    GAMMA_EVAL_VOXEL_MM: float = 1.0
    DOSE_THRESHOLD_PERCENT: float = 10.0

    COMPLEXITY_SAS_THRESHOLD: float = 0.005
    PIPELINE_AUTO_RUN: bool = True


settings = Settings()


def get_env_file_path() -> Path:
    """Resolve .env location in current or backend directory."""
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).parent / ".env",
        Path(__file__).parent.parent / ".env",
    ]
    for c in candidates:
        if c.exists():
            return c
    return Path(__file__).parent / ".env"


def save_settings_to_env(updates: dict[str, Any]) -> Path:
    """Save key/value settings updates into .env file."""
    env_path = get_env_file_path()
    lines: list[str] = []
    existing_keys: set[str] = set()

    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _ = line.split("=", 1)
                    k = k.strip()
                    if k in updates:
                        val = updates[k]
                        lines.append(f"{k}={val}\n")
                        existing_keys.add(k)
                        continue
                lines.append(raw_line)

    for k, v in updates.items():
        if k not in existing_keys and v is not None:
            lines.append(f"{k}={v}\n")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    return env_path


def update_runtime_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Update settings in-memory and write to .env."""
    applied: dict[str, Any] = {}
    for k, v in updates.items():
        if hasattr(settings, k):
            cur = getattr(settings, k)
            val = v
            if cur is not None and val is not None:
                try:
                    if isinstance(cur, bool):
                        if isinstance(val, str):
                            val = val.lower() in ("1", "true", "yes", "on")
                        else:
                            val = bool(val)
                    elif isinstance(cur, int):
                        val = int(val)
                    elif isinstance(cur, float):
                        val = float(val)
                    elif isinstance(cur, str):
                        val = str(val)
                except Exception:
                    pass
            setattr(settings, k, val)
            applied[k] = val

    if applied:
        save_settings_to_env(applied)
    return applied
