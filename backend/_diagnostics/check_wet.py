"""
check_wet.py — quantify the couch's water-equivalent thickness (WET) along the
LP beam direction, comparing (a) the RAW CT from the DICOM store with (b) the
overridden CT.mhd that MCsquare actually simulated on.

LP's dose offset is pure range overshoot along the beam axis (gantry 155):
~12.5 mm without couch override, ~25 mm with it. Overshoot = MC path has LESS
water-equivalent material than TPS. This script measures how much WET the
override ADDED or REMOVED in the couch region, ending the speculation.

Run from backend\:   ..\python\python.exe check_wet.py
"""
import os
import sys
import numpy as np

sys.path.insert(0, ".")

STORE = r"data\dicom_store\520808\1.2.752.243.1.1.20260507125304748.2000.83735"
MHD = r"data\results\plan_3\mcSquare_work\CT.mhd"
CURVE = r"..\MCsquare\Scanners\default\HU_Density_Conversion.txt"

GANTRY_DEG = 155.0  # LP


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


def hu_to_density(img, hu_c, dens_c):
    return np.interp(img, hu_c, dens_c)


def load_raw_ct(store):
    import pydicom, glob
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
                     for d in slices]).astype(np.float32)  # (row=y, col=x, z)
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
    img = raw.reshape(dims, order="F")  # (x, y, z)
    img = np.transpose(img, (1, 0, 2))  # (y, x, z) to match raw CT layout
    return img.astype(np.float32)


def wet_profile(img, ps, hu_c, dens_c, label=""):
    """
    March lines parallel to the LP beam axis (axial plane) from the posterior
    face of the image, integrating density * step over the first 90 mm — the
    band containing the couch. Reports mean WET across a central bundle of rays.
    """
    ny, nx, nz = img.shape
    dens_vol = hu_to_density(img, hu_c, dens_c)
    zc = nz // 2
    sl = dens_vol[:, :, zc]  # (y, x)

    g = np.deg2rad(GANTRY_DEG)
    # beam travel direction (into patient) in DICOM axial coords (x, y):
    # gantry 0 = from anterior traveling +y; gantry g: d = (-sin g, cos g)
    dvec = np.array([-np.sin(g), np.cos(g)])
    step_mm = 0.5
    dstep = dvec * step_mm

    wets = []
    y_start = (ny - 2) * ps[1]
    for x0_idx in range(nx // 2 - 60, nx // 2 + 60, 4):
        x_mm = x0_idx * ps[0]
        y_mm = y_start
        wet = 0.0
        traveled = 0.0
        while traveled < 90.0:
            xi = int(round(x_mm / ps[0]))
            yi = int(round(y_mm / ps[1]))
            if xi < 0 or xi >= nx or yi < 0 or yi >= ny:
                break
            wet += sl[yi, xi] * step_mm
            # march ANTERIORLY along the beam (reverse of travel dir = -d... the
            # beam travels toward -y here since cos155<0, so following dvec
            # already moves anteriorly)
            x_mm += dstep[0]
            y_mm += dstep[1]
            traveled += step_mm
        wets.append(wet)
    wets = np.array(wets)
    print(f"{label}: mean WET over first 90mm from posterior face = "
          f"{wets.mean():6.1f} mm  (min {wets.min():.1f}, max {wets.max():.1f})")
    return wets.mean()


def main():
    hu_c, dens_c = load_curve(CURVE)

    raw_img, raw_ipp, raw_ps = load_raw_ct(STORE)
    print(f"raw CT : shape={raw_img.shape} HU {raw_img.min():.0f}..{raw_img.max():.0f}")

    mhd_img = load_mhd(MHD)
    print(f"CT.mhd : shape={mhd_img.shape} HU {mhd_img.min():.0f}..{mhd_img.max():.0f}")
    # export_CT_for_MCsquare flips Y — un-flip so both are compared in the same frame
    mhd_unflipped = mhd_img[::-1, :, :]

    w_raw = wet_profile(raw_img, raw_ps, hu_c, dens_c, label="RAW CT    ")
    w_ovr = wet_profile(mhd_unflipped, raw_ps, hu_c, dens_c, label="OVERRIDDEN")

    print()
    print(f"WET change from override: {w_ovr - w_raw:+.1f} mm water-equivalent")
    print("If NEGATIVE ~ -10 to -15 mm: the override REMOVED couch material from")
    print("the beam path (air cores wiped the real imaged couch) — that is the")
    print("bug, and matches the overshoot doubling. If ~0 or POSITIVE: the couch")
    print("band is not where the WET went missing, and the Y-flip/frame handling")
    print("of the painted region needs checking instead.")


if __name__ == "__main__":
    main()
