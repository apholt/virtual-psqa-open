"""
Settings endpoint -- exposes and manages runtime configuration and custom pathways.
Allows viewing, validating, and modifying custom pathways and execution parameters
across different operating systems and directory layouts.
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings, update_runtime_settings, get_env_file_path

router = APIRouter(prefix="/api/settings", tags=["settings"])


class PathValidationRequest(BaseModel):
    path: str
    kind: str = "any"  # "file", "dir", "executable", "any"


class PathValidationResponse(BaseModel):
    path: str
    exists: bool
    resolved_path: str
    is_file: bool = False
    is_dir: bool = False
    is_executable: bool = False
    readable: bool = False
    writable: bool = False
    error: Optional[str] = None
    suggestions: list[str] = []


class UpdateSettingsRequest(BaseModel):
    paths: Optional[dict[str, Optional[str]]] = None
    mcsquare: Optional[dict[str, Any]] = None
    gamma_thresholds: Optional[dict[str, Any]] = None
    pipeline: Optional[dict[str, Any]] = None
    orthanc: Optional[dict[str, Any]] = None


def _check_path_status(path_str: Optional[str], kind: str = "any") -> dict[str, Any]:
    if not path_str:
        return {
            "path": "",
            "exists": False,
            "resolved_path": "",
            "is_file": False,
            "is_dir": False,
            "is_executable": False,
            "readable": False,
            "writable": False,
            "error": "Path not set",
            "suggestions": [],
        }

    p = Path(path_str).expanduser()
    resolved = str(p.resolve())
    exists = p.exists()

    suggestions: list[str] = []
    if not exists:
        # Check case variations on Linux
        parent = p.parent
        if parent.exists():
            for sibling in parent.iterdir():
                if sibling.name.lower() == p.name.lower():
                    suggestions.append(str(sibling))
        # Check in common project root locations
        root = Path(__file__).resolve().parent.parent.parent
        candidate = root / p.name
        if candidate.exists() and str(candidate) not in suggestions:
            suggestions.append(str(candidate))

    is_file = p.is_file() if exists else False
    is_dir = p.is_dir() if exists else False
    is_exec = os.access(str(p), os.X_OK) if exists else False
    readable = os.access(str(p), os.R_OK) if exists else False
    writable = os.access(str(p), os.W_OK) if exists else False

    err = None
    if not exists:
        err = "Path does not exist"
    elif kind == "file" and not is_file:
        err = "Expected a file, but found a directory"
    elif kind == "dir" and not is_dir:
        err = "Expected a directory, but found a file"
    elif kind == "executable" and not (is_exec or is_file):
        err = "File exists but is not executable"

    return {
        "path": path_str,
        "exists": exists,
        "resolved_path": resolved,
        "is_file": is_file,
        "is_dir": is_dir,
        "is_executable": is_exec,
        "readable": readable,
        "writable": writable,
        "error": err,
        "suggestions": suggestions,
    }


def _autodetect_system_components() -> dict[str, Any]:
    """Scan directory structure for MCsquare binaries, BDLs, Scanners, etc."""
    cur = Path.cwd()
    root = Path(__file__).resolve().parent.parent.parent
    candidates = [root, cur, cur.parent]

    found_homes: list[str] = []
    found_binaries: list[str] = []
    found_bdls: list[str] = []
    found_scanners: list[str] = []

    is_win = platform.system().lower() == "windows"

    for c in candidates:
        for m_name in ["MCsquare", "mcSquare", "mcsquare"]:
            m_dir = c / m_name
            if m_dir.is_dir():
                found_homes.append(str(m_dir.resolve()))
                # Look for binaries
                for item in m_dir.iterdir():
                    if is_win and item.name.lower().endswith(".exe"):
                        found_binaries.append(str(item.resolve()))
                    elif not is_win and not item.name.lower().endswith(".exe") and not item.name.lower().endswith(".dll") and not item.name.lower().endswith(".bat") and not item.name.lower().endswith(".txt") and item.is_file():
                        if "MCsquare" in item.name or "mc" in item.name.lower():
                            found_binaries.append(str(item.resolve()))

                # Look for BDL
                bdl_dir = m_dir / "BDL"
                if bdl_dir.is_dir():
                    for b in bdl_dir.glob("*.txt"):
                        found_bdls.append(str(b.resolve()))

                # Look for Scanners
                sc_dir = m_dir / "Scanners"
                if sc_dir.is_dir():
                    for s in sc_dir.iterdir():
                        if s.is_dir():
                            found_scanners.append(s.name)

    from config import _is_binary_compatible

    unique_binaries = list(dict.fromkeys(found_binaries))
    unique_binaries.sort(key=lambda b: 0 if _is_binary_compatible(Path(b)) else 1)

    return {
        "mcsquare_homes": list(dict.fromkeys(found_homes)),
        "binaries": unique_binaries,
        "bdl_files": list(dict.fromkeys(found_bdls)),
        "scanners": list(dict.fromkeys(found_scanners)),
    }


@router.get("")
def get_settings():
    path_checks = {
        "mcsquare_home": _check_path_status(settings.MCSQUARE_HOME, "dir"),
        "mcsquare_binary": _check_path_status(settings.MCSQUARE_BINARY, "executable"),
        "mcsquare_bdl_file": _check_path_status(settings.MCSQUARE_BDL_FILE, "file"),
        "mcsquare_bdl_path": _check_path_status(settings.MCSQUARE_BDL_PATH, "dir"),
        "mcsquare_hu_density_file": _check_path_status(settings.MCSQUARE_HU_DENSITY_FILE, "file"),
        "mcsquare_hu_material_file": _check_path_status(settings.MCSQUARE_HU_MATERIAL_FILE, "file"),
        "dicom_watch_folder": _check_path_status(settings.DICOM_WATCH_FOLDER, "dir") if settings.DICOM_WATCH_FOLDER else None,
        "dicom_store_path": _check_path_status(settings.DICOM_STORE_PATH, "dir"),
        "results_path": _check_path_status(settings.RESULTS_PATH, "dir"),
    }

    autodetect = _autodetect_system_components()

    return {
        "system": {
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "working_directory": str(Path.cwd()),
            "env_file": str(get_env_file_path()),
        },
        "paths": {
            "mcsquare_home": settings.MCSQUARE_HOME,
            "mcsquare_binary": settings.MCSQUARE_BINARY,
            "mcsquare_exe": getattr(settings, "MCSQUARE_EXE", "MCsquare_linux_avx2" if platform.system().lower() != "windows" else "MCsquare_win_avx2.exe"),
            "mcsquare_bdl_file": settings.MCSQUARE_BDL_FILE,
            "mcsquare_bdl_path": settings.MCSQUARE_BDL_PATH,
            "mcsquare_hu_density_file": settings.MCSQUARE_HU_DENSITY_FILE,
            "mcsquare_hu_material_file": settings.MCSQUARE_HU_MATERIAL_FILE,
            "dicom_watch_folder": settings.DICOM_WATCH_FOLDER,
            "dicom_store_path": settings.DICOM_STORE_PATH,
            "results_path": settings.RESULTS_PATH,
            "database_url": settings.DATABASE_URL,
        },
        "path_status": path_checks,
        "autodetect": autodetect,
        "mcsquare": {
            "primaries": settings.MCSQUARE_PRIMARIES,
            "num_threads": settings.MCSQUARE_NUM_THREADS,
            "stat_uncertainty": settings.MCSQUARE_STAT_UNCERTAINTY,
            "dose_to_water": settings.MCSQUARE_DOSE_TO_WATER,
            "geometry": settings.MCSQUARE_GEOMETRY,
            "bdl_name": getattr(settings, "MCSQUARE_BDL_NAME", "auto"),
            "scanner": getattr(settings, "MCSQUARE_SCANNER", "default"),
            "rbe": getattr(settings, "PROTON_RBE", 1.10),
            "simulation_mode": getattr(settings, "MCSQUARE_SIMULATION_MODE", False),
            "mock_noise": settings.MCSQUARE_MOCK_NOISE,
        },
        "gamma_thresholds": {
            "mcSquare_vs_TPS": {
                "dd_percent": settings.GAMMA_MCSQUARE_VS_TPS_DD,
                "dta_mm": settings.GAMMA_MCSQUARE_VS_TPS_DTA,
                "pass_threshold": settings.GAMMA_MCSQUARE_VS_TPS_THRESHOLD,
            },
            "log_vs_TPS": {
                "dd_percent": settings.GAMMA_LOG_VS_TPS_DD,
                "dta_mm": settings.GAMMA_LOG_VS_TPS_DTA,
                "pass_threshold": settings.GAMMA_LOG_VS_TPS_THRESHOLD,
            },
            "concordance": {
                "dd_percent": settings.GAMMA_CONCORDANCE_DD,
                "dta_mm": settings.GAMMA_CONCORDANCE_DTA,
                "pass_threshold": settings.GAMMA_CONCORDANCE_THRESHOLD,
            },
            "dose_threshold_percent": settings.DOSE_THRESHOLD_PERCENT,
            "eval_voxel_mm": settings.GAMMA_EVAL_VOXEL_MM,
        },
        "pipeline": {
            "pipeline_auto_run": settings.PIPELINE_AUTO_RUN,
            "dicom_watch_recursive": settings.DICOM_WATCH_RECURSIVE,
            "dicom_settle_seconds": settings.DICOM_SETTLE_SECONDS,
        },
        "orthanc": {
            "orthanc_url": settings.ORTHANC_URL,
            "orthanc_username": settings.ORTHANC_USERNAME,
            "has_password": bool(settings.ORTHANC_PASSWORD),
            "orthanc_timeout_seconds": settings.ORTHANC_TIMEOUT_SECONDS,
        },
    }


@router.post("/validate-path", response_model=PathValidationResponse)
def validate_path_endpoint(req: PathValidationRequest):
    res = _check_path_status(req.path, req.kind)
    return PathValidationResponse(**res)


@router.post("/autodetect")
def autodetect_endpoint():
    return _autodetect_system_components()


@router.post("")
def update_settings_endpoint(req: UpdateSettingsRequest):
    updates: dict[str, Any] = {}

    if req.paths:
        mapping = {
            "mcsquare_home": "MCSQUARE_HOME",
            "mcsquare_binary": "MCSQUARE_BINARY",
            "mcsquare_exe": "MCSQUARE_EXE",
            "mcsquare_bdl_file": "MCSQUARE_BDL_FILE",
            "mcsquare_bdl_path": "MCSQUARE_BDL_PATH",
            "mcsquare_hu_density_file": "MCSQUARE_HU_DENSITY_FILE",
            "mcsquare_hu_material_file": "MCSQUARE_HU_MATERIAL_FILE",
            "dicom_watch_folder": "DICOM_WATCH_FOLDER",
            "dicom_store_path": "DICOM_STORE_PATH",
            "results_path": "RESULTS_PATH",
            "database_url": "DATABASE_URL",
        }
        for k, v in req.paths.items():
            if k in mapping:
                updates[mapping[k]] = v

    if req.mcsquare:
        mapping = {
            "primaries": "MCSQUARE_PRIMARIES",
            "num_threads": "MCSQUARE_NUM_THREADS",
            "stat_uncertainty": "MCSQUARE_STAT_UNCERTAINTY",
            "dose_to_water": "MCSQUARE_DOSE_TO_WATER",
            "geometry": "MCSQUARE_GEOMETRY",
            "bdl_name": "MCSQUARE_BDL_NAME",
            "scanner": "MCSQUARE_SCANNER",
            "rbe": "PROTON_RBE",
            "simulation_mode": "MCSQUARE_SIMULATION_MODE",
            "mock_noise": "MCSQUARE_MOCK_NOISE",
        }
        for k, v in req.mcsquare.items():
            if k in mapping:
                updates[mapping[k]] = v

    if req.gamma_thresholds:
        mc = req.gamma_thresholds.get("mcSquare_vs_TPS", {})
        if "dd_percent" in mc:
            updates["GAMMA_MCSQUARE_VS_TPS_DD"] = float(mc["dd_percent"])
        if "dta_mm" in mc:
            updates["GAMMA_MCSQUARE_VS_TPS_DTA"] = float(mc["dta_mm"])
        if "pass_threshold" in mc:
            updates["GAMMA_MCSQUARE_VS_TPS_THRESHOLD"] = float(mc["pass_threshold"])

        log_gamma = req.gamma_thresholds.get("log_vs_TPS", {})
        if "dd_percent" in log_gamma:
            updates["GAMMA_LOG_VS_TPS_DD"] = float(log_gamma["dd_percent"])
        if "dta_mm" in log_gamma:
            updates["GAMMA_LOG_VS_TPS_DTA"] = float(log_gamma["dta_mm"])
        if "pass_threshold" in log_gamma:
            updates["GAMMA_LOG_VS_TPS_THRESHOLD"] = float(log_gamma["pass_threshold"])

        if "dose_threshold_percent" in req.gamma_thresholds:
            updates["DOSE_THRESHOLD_PERCENT"] = float(req.gamma_thresholds["dose_threshold_percent"])
        if "eval_voxel_mm" in req.gamma_thresholds:
            updates["GAMMA_EVAL_VOXEL_MM"] = float(req.gamma_thresholds["eval_voxel_mm"])

    if req.pipeline:
        if "pipeline_auto_run" in req.pipeline:
            updates["PIPELINE_AUTO_RUN"] = bool(req.pipeline["pipeline_auto_run"])
        if "dicom_watch_recursive" in req.pipeline:
            updates["DICOM_WATCH_RECURSIVE"] = bool(req.pipeline["dicom_watch_recursive"])
        if "dicom_settle_seconds" in req.pipeline:
            updates["DICOM_SETTLE_SECONDS"] = int(req.pipeline["dicom_settle_seconds"])

    if req.orthanc:
        if "orthanc_url" in req.orthanc:
            updates["ORTHANC_URL"] = str(req.orthanc["orthanc_url"] or "").rstrip("/")
        if "orthanc_username" in req.orthanc:
            updates["ORTHANC_USERNAME"] = req.orthanc["orthanc_username"] or None
        if "orthanc_password" in req.orthanc and req.orthanc["orthanc_password"] is not None:
            # Only update if non-empty string or explicitly cleared
            updates["ORTHANC_PASSWORD"] = req.orthanc["orthanc_password"] or None
        if "orthanc_timeout_seconds" in req.orthanc and req.orthanc["orthanc_timeout_seconds"]:
            try:
                updates["ORTHANC_TIMEOUT_SECONDS"] = int(req.orthanc["orthanc_timeout_seconds"])
            except ValueError:
                pass

    applied = update_runtime_settings(updates)
    return {
        "status": "success",
        "applied": applied,
        "env_file": str(get_env_file_path()),
    }


@router.post("/test-orthanc")
def test_orthanc_endpoint(payload: Optional[dict] = None):
    """Test connection to Orthanc with optional override URL/credentials."""
    from services.orthanc_service import check_orthanc_connection
    url = (payload.get("orthanc_url") or payload.get("url")) if payload else None
    user = (payload.get("orthanc_username") or payload.get("username")) if payload else None
    pw = (payload.get("orthanc_password") or payload.get("password")) if payload else None
    return check_orthanc_connection(url=url, username=user, password=pw)

