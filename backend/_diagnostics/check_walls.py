"""
check_walls.py — print HU y-profiles from the CT.mhd MCsquare simulated on, at
the x/z where LP's central ray crosses the couch, in the file's OWN frame (no
un-flip). Shows whether the painted shell walls (8000/8001) actually sit on the
beam path, or the beam threads between/past them.

Run from backend\:   ..\python\python.exe check_walls.py
"""
import os
import numpy as np

MHD = r"data\results\plan_3\mcSquare_work\CT.mhd"

# LP crossing geometry (from the plan / earlier checks):
#   isocenter (x,y,z) = (22.99, -191.03, 109.36) mm
#   CT: IPP=(-249.51, -448.01, -54.0), spacing=(0.9766, 0.9766, 2.0)
#   beam travel dir (x,y) at gantry 155 = (-0.4226, -0.9063); entry is BEHIND
#   the patient (posterior, larger y in patient frame).
IPP = (-249.51171875, -448.01171875, -54.0)
PS = (0.9765625, 0.9765625, 2.0)
ISO = (22.98993, -191.0327, 109.3624)


def load_mhd(path):
    hdr = {}
    for line in open(path):
        if "=" in line:
            k, v = line.split("=", 1)
            hdr[k.strip()] = v.strip()
    dims = [int(x) for x in hdr["DimSize"].split()]
    tmap = {"MET_FLOAT": np.float32, "MET_SHORT": np.int16}
    raw = np.fromfile(os.path.join(os.path.dirname(path), hdr["ElementDataFile"]),
                      dtype=tmap[hdr["ElementType"]])
    img = raw.reshape(dims, order="F")   # (x, y, z) as stored
    return img, dims


def main():
    img, dims = load_mhd(MHD)   # img[x, y, z]
    nx, ny, nz = dims
    zc = int(round((ISO[2] - IPP[2]) / PS[2]))
    xi_iso = int(round((ISO[0] - IPP[0]) / PS[0]))
    print(f"CT.mhd dims={dims}  iso slice z={zc}  iso x index={xi_iso}")

    # LP travels (-0.4226, -0.9063): entry side is +y in patient frame. In the
    # exported (Y-flipped) frame, the couch is near the OTHER y edge. We don't
    # assume — we scan BOTH y edges at several x positions around the beam's
    # couch-crossing x. The beam moves in -x as it goes back toward entry, so
    # the crossing x is a bit larger than iso x; sample a range.
    print("\nWhere are the painted 8000/8001 voxels in this frame?")
    wall = (img == 8000) | (img == 8001)
    ys = np.where(wall.any(axis=(0, 2)))[0]
    print(f"  wall voxels span y indices {ys.min()}..{ys.max()} of {ny}")
    xs = np.where(wall.any(axis=(1, 2)))[0]
    print(f"  wall voxels span x indices {xs.min()}..{xs.max()} of {nx}")

    air = img <= -990
    # profile: for a few x near the beam's couch crossing, print the y-column
    # segment covering the couch band (where the walls are)
    y0 = max(0, ys.min() - 8)
    y1 = min(ny, ys.max() + 9)
    print(f"\nHU y-profiles at iso slice z={zc} (y {y0}..{y1}):")
    for dx in (0, 30, 60, 90, 120):
        xi = xi_iso + dx     # beam moves toward +x going back to entry? print a spread
        if xi < 0 or xi >= nx:
            continue
        col = img[xi, y0:y1, zc]
        # compress: show runs
        vals = []
        prev = None
        run = 0
        for v in col:
            tag = ("8001" if v == 8001 else "8000" if v == 8000 else
                   "air" if v <= -990 else "foam" if v < -700 else
                   "tissue" if -200 < v < 200 else f"{int(v)}")
            if tag == prev:
                run += 1
            else:
                if prev is not None:
                    vals.append(f"{prev}x{run}")
                prev, run = tag, 1
        vals.append(f"{prev}x{run}")
        print(f"  x={xi:4d}: " + " | ".join(vals))

    # how many wall voxels lie on the iso slice at all?
    n_wall_slice = int(wall[:, :, zc].sum())
    print(f"\nwall voxels on iso slice z={zc}: {n_wall_slice}")
    # and on neighbours
    for dz in (-2, -1, 1, 2):
        z = zc + dz
        if 0 <= z < nz:
            print(f"wall voxels on slice z={z}: {int(wall[:, :, z].sum())}")


if __name__ == "__main__":
    main()
