"""
check_couch.py — verify the couch density override lands correctly on the CT.

Loads the CT, applies the density override, and reports per-structure voxel
counts + the HU actually written, plus a couch-region HU profile before/after
on a slice the LP beam crosses. Run from backend\:
    ..\python\python.exe check_couch.py
"""
import sys
sys.path.insert(0, ".")
import os
import numpy as np

ROOT = os.path.abspath("..")
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

from Process.PatientData import PatientList

STORE = r"backend\data\dicom_store\520808\1.2.752.243.1.1.20260507125304748.2000.83735"
HU_FILE = r"MCsquare\Scanners\default\HU_Density_Conversion.txt"


def main():
    pl = PatientList()
    pl.list_dicom_files(STORE, 1)
    pat = pl.list[0]
    pat.RTdoses = []
    pat.import_patient_data()
    CT = pat.CTimages[0]
    print("CT grid", CT.GridSize, "spacing", CT.PixelSpacing)
    print("CT HU range before:", float(CT.Image.min()), "to", float(CT.Image.max()))

    # snapshot before
    before = CT.Image.copy()

    from ct_density_override import apply_density_overrides

    # find the RTSTRUCT in the store
    import pydicom, glob
    rs = None
    for pth in glob.glob(os.path.join(STORE, "*.dcm")):
        try:
            dc = pydicom.dcmread(pth, stop_before_pixels=True, force=True)
        except Exception:
            continue
        if str(getattr(dc, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.481.3":
            rs = pth
            break
    if rs is None:
        print("no RTSTRUCT found")
        return
    n = apply_density_overrides(CT, rs, HU_FILE, log=print)
    print("structures applied:", n)

    # where did HU actually change?
    changed = CT.Image != before
    print("total changed voxels:", int(changed.sum()))
    # per-slice count of changed voxels (couch should span most z-slices)
    per_z = changed.reshape(-1, changed.shape[2]).sum(axis=0)
    nz = changed.shape[2]
    z_with = int((per_z > 0).sum())
    print(f"z-slices with any override: {z_with} / {nz}")
    print("first/last override z-index:",
          int(np.argmax(per_z > 0)), int(nz - 1 - np.argmax(per_z[::-1] > 0)))

    # HU histogram of the changed voxels — should be ~1521 (shell) and ~-1138 (air)
    vals = CT.Image[changed]
    for lo, hi, lab in [(-1200, -1000, "air core"), (-100, 200, "qfix shell"),
                        (1400, 1600, "medphoton shell")]:
        print(f"  HU {lab:16s} [{lo},{hi}]: {int(((vals>=lo)&(vals<hi)).sum())} voxels")


if __name__ == "__main__":
    main()