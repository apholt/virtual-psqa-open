"""
check_axis_wet.py — integrate water-equivalent thickness along LP's TRUE central
axis (through the isocenter, gantry 155) from the isocenter back to the entry
side, for (a) the raw CT and (b) the overridden CT.mhd MCsquare simulated on.

This is the exact material LP's central ray traverses. Decides between:
  - overridden WET > raw WET on this line, yet range got DEEPER
       -> MCsquare provably not transporting through the painted region
  - overridden WET < raw WET on this line (rasterization hole at the crossing)
       -> the override has a gap where the beam crosses; that's the bug

Run from backend\:   ..\python\python.exe check_axis_wet.py
"""
import os
import sys
import glob
import numpy as np
import pydicom

sys.path.insert(0, ".")

STORE = r"data\dicom_store\520808\1.2.752.243.1.1.20260507125304748.2000.83735"
MHD = r"data\results\plan_3\mcSquare_work\CT.mhd"
CURVE = r"..\MCsquare\Scanners\default\HU_Density_Conversion.txt"
GANTRY_DEG = 155.0  # LP
STEP = 0.5          # mm
MAX_LEN = 250.0     # mm from isocenter back toward entry


def load_curve(path):
    hu, dens = [], []
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            hu.append(float(parts[0])); dens.append(float(parts[1]))
    hu = np.array(hu); dens = np.array(dens)
    order = np.argsort(hu)
    return hu[order], dens[order]


def load_raw_ct(store):
    slices = []
    for p in glob.glob(os.path.join(store, "*.dcm")):
        try:
            d = pydicom.dcmread(p, force=True)
        except Exception:
            continue
        if str(getattr(d, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.2":
            slices.append(d)
    slices.sort(key=lambda d: float(d.ImagePositionPatient[2]))
    img = np.dstack([d.pixel_array * d.RescaleSlope + d.RescaleIntercept
                     for d in slices]).astype(np.float32)  # (y, x, z)
    d0 = slices[0]
    ipp = [float(d0.ImagePositionPatient[0]), float(d0.ImagePositionPatient[1]),
           float(d0.ImagePositionPatient[2])]
    ps = [float(d0.PixelSpacing[0]), float(d0.PixelSpacing[1]),
          float(slices[1].ImagePositionPatient[2]) - float(slices[0].ImagePositionPatient[2])]
    return img, ipp, ps


def load_mhd(path):
    hdr = {}
    for line in open(path):
        if "=" in line:
            k, v = line.split("=", 1)
            hdr[k.strip()] = v.strip()
    dims = [int(x) for x in hdr["DimSize"].split()]
    tmap = {"MET_FLOAT": np.float32, "MET_SHORT": np.int16, "MET_DOUBLE": np.float64}
    raw = np.fromfile(os.path.join(os.path.dirname(path), hdr["ElementDataFile"]),
                      dtype=tmap[hdr["ElementType"]])
    img = raw.reshape(dims, order="F")     # (x, y, z)
    img = np.transpose(img, (1, 0, 2))     # (y, x, z)
    return img.astype(np.float32)


def main():
    hu_c, dens_c = load_curve(CURVE)
    raw, ipp, ps = load_raw_ct(STORE)
    mhd = load_mhd(MHD)[::-1, :, :]   # un-flip Y so frames match raw CT

    rp = [x for x in glob.glob(os.path.join(STORE, "*.dcm"))
          if os.path.basename(x).startswith("RP")][0]
    d = pydicom.dcmread(rp)
    beam = None
    for b in d.IonBeamSequence:
        gs = [float(cp.GantryAngle) for cp in b.IonControlPointSequence
              if "GantryAngle" in cp]
        if gs and abs(gs[0] - GANTRY_DEG) < 0.5:
            beam = b
            break
    iso = [float(v) for v in beam.IonControlPointSequence[0].IsocenterPosition]
    print(f"LP beam '{beam.BeamName}' isocenter (x,y,z mm): {iso}")

    g = np.deg2rad(GANTRY_DEG)
    dvec = np.array([-np.sin(g), np.cos(g)])   # beam TRAVEL direction (x, y)
    zc = int(round((iso[2] - ipp[2]) / ps[2]))
    print(f"axial slice index at isocenter z: {zc}")

    def axis_wet(img, label):
        sl = np.interp(img[:, :, zc], hu_c, dens_c)   # density slice (y, x)
        # walk BACK toward the entry: opposite of travel direction
        x = iso[0] - ipp[0]
        y = iso[1] - ipp[1]
        wet = 0.0
        profile = []
        t = 0.0
        while t < MAX_LEN:
            xi = int(round(x / ps[0]))
            yi = int(round(y / ps[1]))
            if xi < 0 or xi >= sl.shape[1] or yi < 0 or yi >= sl.shape[0]:
                break
            dens = sl[yi, xi]
            wet += dens * STEP
            profile.append((t, dens))
            x -= dvec[0] * STEP
            y -= dvec[1] * STEP
            t += STEP
        print(f"{label}: WET isocenter->entry along LP axis = {wet:6.1f} mm "
              f"(path length {t:.0f} mm)")
        return wet, profile

    w_raw, p_raw = axis_wet(raw, "RAW CT    ")
    w_ovr, p_ovr = axis_wet(mhd, "OVERRIDDEN")
    print(f"\ncentral-axis WET change from override: {w_ovr - w_raw:+.1f} mm")

    # show where along the ray the two differ (the couch crossing)
    print("\ndensity along ray where |raw - overridden| > 0.05 (t mm from iso):")
    diffs = [(t, dr, do) for (t, dr), (_, do) in zip(p_raw, p_ovr)
             if abs(dr - do) > 0.05]
    if not diffs:
        print("  none — the two CTs are identical along this ray (!)")
    else:
        t0, t1 = diffs[0][0], diffs[-1][0]
        print(f"  differs from t={t0:.1f} to t={t1:.1f} mm behind isocenter")
        for t, dr, do in diffs[:: max(1, len(diffs)//12)]:
            print(f"    t={t:6.1f}  raw={dr:6.3f}  overridden={do:6.3f}")


if __name__ == "__main__":
    main()
