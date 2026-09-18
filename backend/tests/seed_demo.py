"""
Seed a few demo plans through the real pipeline so the UI has something to show
during a walkthrough. Bootstrap mode (no trained model) is the correct first-run
state, so every plan will show an ML verdict of 'measure'.

Run from backend/:  python -m tests.seed_demo
"""
from __future__ import annotations

import tempfile

from database import SessionLocal
from services.dicom_ingestor import ingest_dicom_directory
from services.pipeline import run_stage1, run_stage2
from tests.synthetic_dicom_gen import write_synthetic_dicom_set

# (n_fields, n_fractions_to_deliver) per demo plan.
DEMO_PLANS = [
    (2, 0),   # plan QA only (Stage 1)
    (3, 2),   # two fractions delivered (Stage 1 + Stage 2)
    (2, 3),   # three fractions delivered
    (4, 0),   # complex 4-field plan, plan QA only
]


def main() -> None:
    for i, (n_fields, n_frac) in enumerate(DEMO_PLANS, start=1):
        db = SessionLocal()
        tmp = tempfile.mkdtemp(prefix=f"demo_{i}_")
        write_synthetic_dicom_set(tmp, n_fields=n_fields)
        result = ingest_dicom_directory(tmp, db)
        plan_id = result["plan_id"]
        print(f"[{i}] ingested plan {plan_id} "
              f"({result['patient_name']}, {n_fields} fields)")

        run_stage1(plan_id, background=False)
        for fx in range(1, n_frac + 1):
            run_stage2(plan_id, fraction_number=fx, background=False)
        print(f"    Stage 1 done; {n_frac} fraction(s) delivered")
        db.close()

    print("\nDemo data seeded. Open http://localhost:8000")


if __name__ == "__main__":
    main()
