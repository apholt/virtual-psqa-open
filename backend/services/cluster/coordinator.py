"""
Cluster Coordinator for Distributed MCsquare Calculations.
Partitions multi-beam proton plans, maps beams to available idle cluster nodes,
orchestrates concurrent execution, handles automatic local failover, and
aggregates results into a composite dose grid.
"""
from __future__ import annotations

import concurrent.futures
import io
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional, Callable
import httpx
import numpy as np

from config import settings
from services.dose_grid import DoseGrid
from models.qa_job import QAJob
from services.cluster.models import BeamTaskRequest, BeamTaskResult, ClusterNode
from services.cluster.node_registry import get_node_registry

logger = logging.getLogger(__name__)


class ClusterCoordinator:
    def __init__(self):
        self.registry = get_node_registry()

    def is_cluster_available(self) -> bool:
        """Returns True if cluster is enabled and at least one remote node is idle and online."""
        if not settings.CLUSTER_ENABLED:
            return False
        idle_nodes = self.registry.get_available_idle_nodes()
        return len(idle_nodes) > 0

    def dispatch_remote_beam(
        self,
        node: ClusterNode,
        req: BeamTaskRequest,
        ct_mhd_bytes: bytes,
        ct_raw_bytes: bytes,
        output_beam_path: Path,
        progress_callback: Optional[Callable[[float], None]] = None,
        timeout_seconds: float = 1800.0,
    ) -> BeamTaskResult:
        """
        Sends CT geometry and beam parameters to a remote worker node,
        monitors simulation execution, and downloads the resulting dose grid.
        """
        logger.info(
            f"Dispatching Beam {req.beam_no} of Plan {req.plan_id} to node '{node.name}' ({node.url})..."
        )
        start_time = time.monotonic()

        files = {
            "ct_mhd": ("CT.mhd", ct_mhd_bytes, "application/octet-stream"),
            "ct_raw": ("CT.raw", ct_raw_bytes, "application/octet-stream"),
            "plan_pencil": ("PlanPencil.txt", req.plan_pencil_text.encode("utf-8"), "text/plain"),
        }
        data = {
            "job_id": str(req.job_id),
            "plan_id": str(req.plan_id),
            "beam_no": str(req.beam_no),
            "field_index": str(req.field_index),
            "total_fields": str(req.total_fields),
            "delivered_protons": str(req.delivered_protons),
            "primaries": str(req.primaries),
            "uncertainty": str(req.uncertainty),
            "dose_scaling": str(req.dose_scaling),
            "rbe": str(req.rbe),
            "bdl_name": str(req.bdl_name),
            "scanner": str(req.scanner),
        }

        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(f"{node.url}/simulate_beam", data=data, files=files)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Remote node '{node.name}' returned status {resp.status_code}: {resp.text}"
                )

            # The response payload is the compressed .npz dose grid
            dose_bytes = resp.content
            output_beam_path.parent.mkdir(parents=True, exist_ok=True)
            output_beam_path.write_bytes(dose_bytes)

            duration = time.monotonic() - start_time
            # Inspect array
            with np.load(io.BytesIO(dose_bytes)) as z:
                arr = z["array"]
                max_dose = float(arr.max())
                shape = list(arr.shape)

            logger.info(
                f"Successfully completed Beam {req.beam_no} on '{node.name}' "
                f"in {duration:.1f}s (max={max_dose:.4f} Gy)"
            )
            return BeamTaskResult(
                success=True,
                beam_no=req.beam_no,
                max_dose=max_dose,
                dose_shape=shape,
                duration_seconds=duration,
            )

    def simulate_plan_cluster(
        self,
        plan_id: int,
        output_dir: Path,
        job_id: int,
        db,
        force: bool = False,
    ) -> Optional[str]:
        """
        Main entry point for distributed plan simulation.
        Returns the path to the completed composite mc_dose.npz if cluster execution
        succeeded, or None if cluster execution could not be used (triggering local fallback).
        """
        if not self.is_cluster_available():
            return None

        idle_nodes = self.registry.get_available_idle_nodes()
        if not idle_nodes:
            logger.info(f"No idle cluster nodes currently online. Falling back to local execution.")
            return None

        logger.info(
            f"Initiating distributed MCsquare simulation for Plan {plan_id} "
            f"across {len(idle_nodes)} idle node(s)..."
        )

        from models.plan import Plan
        plan = db.query(Plan).filter_by(id=plan_id).first()
        if not plan or not plan.dicom_store_path:
            return None

        # Build work directory and prep CT geometry locally
        backend_dir = Path(__file__).resolve().parent.parent.parent
        work_dir = Path(settings.RESULTS_PATH) / f"plan_{plan_id}" / "mcSquare_work"
        work_dir.mkdir(parents=True, exist_ok=True)

        # Import patient data and CT locally to generate CT.mhd
        import sys
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))

        try:
            from Process.Patient import PatientList
            from Process.MCsquare import MCsquare_simulation
            from Process.Plan import export_plan_for_MCsquare

            patients = PatientList()
            patients.import_patient_data(plan.dicom_store_path)
            patient = patients[0]
            sim_plan = patient.Plans[0]
            ct = patient.CT

            mc2 = MCsquare_simulation(str(work_dir))
            mc2.init_simulation_directory(ct, sim_plan, bdl=getattr(settings, "MCSQUARE_BDL_NAME", "auto"))
        except Exception as prep_exc:
            logger.warning(
                f"Failed to prepare CT geometry locally for cluster dispatch: {prep_exc}. "
                f"Falling back to standard local runner."
            )
            return None

        ct_mhd_path = work_dir / "CT.mhd"
        ct_raw_path = work_dir / "CT.raw"
        if not ct_mhd_path.exists() or not ct_raw_path.exists():
            logger.warning("CT.mhd / CT.raw missing after prep. Falling back to local runner.")
            return None

        ct_mhd_bytes = ct_mhd_path.read_bytes()
        ct_raw_bytes = ct_raw_path.read_bytes()

        # Plan beams
        beams = sim_plan.Beams
        n_beams = len(beams)
        plan_beam_numbers = [getattr(b, "BeamNumber", i + 1) for i, b in enumerate(beams)]

        # Prepare per-beam tasks
        tasks: list[tuple[int, int, BeamTaskRequest, Path]] = []
        for i, beam in enumerate(beams):
            beam_no = plan_beam_numbers[i]
            beam_npz_path = output_dir / f"mc_dose_beam{beam_no}.npz"

            if not force and beam_npz_path.exists():
                logger.info(f"Beam {beam_no} already cached at {beam_npz_path.name}; skipping.")
                continue

            import copy
            sub_plan = copy.copy(sim_plan)
            sub_plan.Beams = [beam]

            plan_pencil_path = work_dir / f"PlanPencil_beam{beam_no}.txt"
            export_plan_for_MCsquare(sub_plan, str(plan_pencil_path), ct, mc2.BDL)
            pencil_text = plan_pencil_path.read_text(encoding="utf-8")

            req = BeamTaskRequest(
                job_id=job_id,
                plan_id=plan_id,
                beam_no=beam_no,
                field_index=i,
                total_fields=n_beams,
                plan_pencil_text=pencil_text,
                delivered_protons=float(sub_plan.DeliveredProtons),
                primaries=settings.MCSQUARE_PRIMARIES,
                uncertainty=settings.MCSQUARE_STAT_UNCERTAINTY,
                dose_scaling=0.9,
                rbe=getattr(settings, "PROTON_RBE", 1.10),
                bdl_name=getattr(settings, "MCSQUARE_BDL_NAME", "auto"),
                scanner=getattr(settings, "MCSQUARE_SCANNER", "default"),
            )
            tasks.append((i, beam_no, req, beam_npz_path))

        if not tasks:
            logger.info("All beams are already cached. Assembling composite dose...")
            return self._sum_existing_beams(output_dir, plan_beam_numbers)

        # Distribute tasks across available nodes
        # If fewer tasks than nodes, pick the fastest/lowest CPU nodes
        sorted_nodes = sorted(idle_nodes, key=lambda n: n.cpu_pct)
        assignments: list[tuple[ClusterNode, BeamTaskRequest, Path]] = []
        for idx, (_, _, req, out_path) in enumerate(tasks):
            assigned_node = sorted_nodes[idx % len(sorted_nodes)]
            assignments.append((assigned_node, req, out_path))

        logger.info(
            f"Assigned {len(tasks)} beam(s) across {min(len(tasks), len(sorted_nodes))} node(s): "
            + ", ".join(f"Beam {r.beam_no} -> {n.name}" for n, r, _ in assignments)
        )

        # Execute concurrently with automatic fallback
        failed_tasks: list[tuple[BeamTaskRequest, Path]] = []
        completed_count = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(assignments)) as executor:
            future_to_beam = {
                executor.submit(
                    self.dispatch_remote_beam,
                    node,
                    req,
                    ct_mhd_bytes,
                    ct_raw_bytes,
                    out_path,
                ): (node, req, out_path)
                for node, req, out_path in assignments
            }

            for future in concurrent.futures.as_completed(future_to_beam):
                node, req, out_path = future_to_beam[future]
                try:
                    res = future.result()
                    if res.success and out_path.exists():
                        completed_count += 1
                        logger.info(
                            f"Cluster beam {req.beam_no} successfully finished ({completed_count}/{len(tasks)})."
                        )
                    else:
                        raise RuntimeError(res.error or "Unknown worker failure")
                except Exception as node_err:
                    logger.warning(
                        f"Remote node '{node.name}' failed on Beam {req.beam_no}: {node_err}. "
                        f"Queuing for local fallback..."
                    )
                    failed_tasks.append((req, out_path))

        # Handle any failed beams locally
        if failed_tasks:
            logger.info(f"Computing {len(failed_tasks)} failed beam(s) on local host...")
            for req, out_path in failed_tasks:
                success = self._compute_beam_locally(
                    plan_id, req, work_dir, out_path, mc2, ct, sim_plan
                )
                if not success:
                    raise RuntimeError(f"Local fallback also failed for Beam {req.beam_no}.")

        # Assemble and sum all beam doses
        return self._sum_existing_beams(output_dir, plan_beam_numbers)

    def _compute_beam_locally(
        self,
        plan_id: int,
        req: BeamTaskRequest,
        work_dir: Path,
        output_beam_path: Path,
        mc2,
        ct,
        sim_plan,
    ) -> bool:
        """Executes a single beam locally as a fallback mechanism."""
        import copy
        import subprocess
        from Process.Plan import export_plan_for_MCsquare
        from Process.MCsquare_config import generate_MCsquare_config, export_MCsquare_config
        from config import _resolve_default_mcsquare_exe

        sub_plan = copy.copy(sim_plan)
        sub_plan.Beams = [sim_plan.Beams[req.field_index]]

        pencil_path = work_dir / "PlanPencil.txt"
        pencil_path.write_text(req.plan_pencil_text, encoding="utf-8")

        # Generate config
        mc2.config = generate_MCsquare_config(
            str(work_dir), req.primaries, mc2.Scanner.get_path(), mc2.BDL.get_path(),
            "CT.mhd", "PlanPencil.txt", True,
        )
        mc2.config["Stat_uncertainty"] = req.uncertainty
        export_MCsquare_config(mc2.config)

        # Run local executable
        install_dir = Path(settings.MCSQUARE_HOME).resolve()
        exe = install_dir / _resolve_default_mcsquare_exe()
        env = os.environ.copy()
        env["MCsquare_Materials_Dir"] = str(install_dir / "Materials")

        proc = subprocess.run(
            [str(exe), "config.txt"],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.returncode != 0:
            logger.error(f"Local fallback execution failed: {proc.stderr}")
            return False

        mhd_b = mc2.import_MCsquare_dose(sub_plan, "Dose_Beam1.mhd", req.dose_scaling)
        if mhd_b is None:
            mhd_b = mc2.import_MCsquare_dose(sub_plan, "Dose.mhd", req.dose_scaling)
        if mhd_b is None:
            return False

        # Convert to DoseGrid npz
        data = mhd_b.Dose
        data_f32 = data.astype("<f4")
        arr = np.transpose(data_f32, (2, 1, 0))
        output_beam_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            output_beam_path,
            array=arr,
            spacing=np.array(mhd_b.VoxelSize[::-1], dtype=np.float32),
            origin=np.array(mhd_b.Offset[::-1], dtype=np.float32),
        )
        return True

    def _sum_existing_beams(self, output_dir: Path, plan_beam_numbers: list[int]) -> str:
        """Sums all beam DoseGrid .npz files into composite mc_dose.npz."""
        summed: Optional[np.ndarray] = None
        ref_spacing = None
        ref_origin = None

        for b_num in plan_beam_numbers:
            p = output_dir / f"mc_dose_beam{b_num}.npz"
            if not p.exists():
                raise FileNotFoundError(f"Missing expected beam dose file: {p.name}")
            with np.load(p) as z:
                arr = z["array"]
                ref_spacing = z["spacing"]
                ref_origin = z["origin"]
                summed = arr.copy() if summed is None else summed + arr

        summed_path = output_dir / "mc_dose.npz"
        np.savez_compressed(
            summed_path,
            array=summed,
            spacing=ref_spacing,
            origin=ref_origin,
        )
        logger.info(f"Composite dose successfully assembled at {summed_path.name}")
        return str(summed_path)
