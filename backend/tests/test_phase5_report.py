"""
Phase 5 report smoke test.

Ingests a synthetic plan, runs Stage 1 (MCsquare + gamma) and Stage 2 (log),
then builds the HTML report and the fractional-trend payload.

Run from backend/:  python -m tests.test_phase5_report
"""
from __future__ import annotations

import tempfile

from database import SessionLocal
from services.dicom_ingestor import ingest_dicom_directory
from services.pipeline import run_stage1, run_stage2
from services.report_generator import build_report_html
from tests.synthetic_dicom_gen import write_synthetic_dicom_set


def main() -> None:
    db = SessionLocal()
    tmp = tempfile.mkdtemp(prefix="phase5_test_")
    write_synthetic_dicom_set(tmp, n_fields=2)
    result = ingest_dicom_directory(tmp, db)
    plan_id = result["plan_id"]
    print(f"[ok] Ingested plan {plan_id}")

    run_stage1(plan_id, background=False)
    print("[ok] Stage 1 complete")
    run_stage2(plan_id, fraction_number=1, background=False)
    run_stage2(plan_id, fraction_number=2, background=False)
    print("[ok] Stage 2 (x2 fractions) complete")

    html = build_report_html(plan_id, db)
    assert "Fractional Delivery QA Analysis" in html, "report header missing"
    assert "Patient Information" in html, "patient info missing"
    assert "Fractional Gamma Passing Rate Trend" in html, "trend section missing"
    assert "Field-by-Field Breakdown" in html, "per-field section missing"
    assert "Field Spatial Dose &amp; Gamma Verification (at Isocenter)" in html, "field spatial dose section missing"
    assert "Secondary Dose &amp; Robustness" not in html, "secondary dose calc should be removed from fractional report"
    assert "ELECTRONIC OMR INTEGRATION" in html, "OMR notice missing"
    assert 'class="sign-line"' not in html, "physical sign-off should be absent"
    print(f"[ok] Report HTML built ({len(html)} bytes)")

    out = tempfile.NamedTemporaryFile(delete=False, suffix=".html").name
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] Report written to {out}")

    print("\nPhase 5 report smoke test PASSED")
    db.close()


def test_phase5_report() -> None:
    main()


if __name__ == "__main__":
    main()
