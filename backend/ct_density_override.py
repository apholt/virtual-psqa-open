"""
ct_density_override.py — apply RTSTRUCT material overrides to the CT so MCsquare
computes through them, matching RayStation.

DENSITYOVERRIDE_V2  (marker for Select-String verification)

RayStation assigns a material/density directly to a contour and ignores the
region's raw CT HU. For MCsquare we must do the equivalent: paint those voxels
with an HU that this scanner's calibration maps to the intended density AND
material.

-------------------------------------------------------------------------------
HU codes — verified against BOTH curves in mcSquare/Scanners/default/
-------------------------------------------------------------------------------
Both files must agree, and this is the trap that bit us before: the MATERIAL
file is a step function, the DENSITY file is piecewise-linear INTERPOLATED.
An HU that appears in one but not the other gets an interpolated density.

    HU     density (interp)   material (step)     used for
    ----   ----------------   -----------------   --------------------------
    0      1.00               17  Water           "Water" overrides
    37     1.05               46  SoftTissus      "Muscle" overrides
    8000   1.20               67  CFRP            Qfix Overlay (Shell)
    8001   2.03               67  CFRP            MedPhoton Couch (Shell)
    8500   19.20              73  Gold            "Gold" overrides (fiducials)

*** FIXED 2026-07-16: "Gold" was previously painted at HU 8040. The MATERIAL
    file lists 8040 -> 73 (Au), so it looked correct — but the DENSITY file has
    NO 8040 entry, so density interpolated between 8001 (2.03) and 8500 (19.2),
    giving 3.37 g/cm3. Gold was being modelled 5.7x too light. HU 8500 gives
    Au at 19.2 g/cm3, which is the intended gold. ***

An earlier version painted a density-derived ~1521 HU for the couch, which maps
to Schneider_Marrow_Bone and STOPPED the proton beam — wrong material and too
dense. The reserved 8000/8001 codes are the correct commissioned representation.

-------------------------------------------------------------------------------
Unhandled materials
-------------------------------------------------------------------------------
Any RayStation material not in SDC_MATERIAL_HU / _couch_hu_for_material is left
as RAW CT — meaning MCsquare transports through different geometry than the TPS
computed, and the resulting gamma is not a valid check. These are now logged
with an UNHANDLED MATERIAL banner and reported via the optional `unhandled_out`
list so the caller can force the plan's verdict to 'measure'.

Known unhandled as of 2026-07-16 (from audit_materials.py over the store):
    'Titanium'  (Magnet_Wings, Sternum Wire)  — 2 plans.  NOT in MCsquare's
                material library at all (see Materials/list.dat: elements jump
                Al -> Si -> P -> Ca -> Fe). Needs a new material folder + ID
                before it can be supported.
    'Iron'      (Magnet_Center)               — 1 plan.  Material ID 9 EXISTS.
                Supportable by adding a reserved code to BOTH scanner curves:
                    HU_Material_Conversion.txt:   8020    9        # Fe
                    HU_Density_Conversion.txt:    8020    7.874
                8020 sits below 8040 so the material step yields Fe, not Au, and
                an exact density point at 8020 avoids the interpolation trap that
                caused the gold bug. This is a COMMISSIONING change to validated
                files — validate (e.g. depth-dose through a known slab) and
                document before clinical use. Then add  "Iron": 8020  below.

Overrides are matched by the structure's material name (0x300a,0x00e1), the same
tag RayStation/SDC use.
"""
from __future__ import annotations

import numpy as np
import pydicom
from PIL import Image, ImageDraw

AIR_HU = -1000

# ---------------------------------------------------------------------------
# Couch / support materials -> reserved CFRP HU codes. Keyed by a normalised
# substring of the RayStation material name so "Shell 2.03", "Qfix 1.200", etc.
# resolve regardless of exact formatting.
# ---------------------------------------------------------------------------
def _couch_hu_for_material(material: str):
    """Return the HU to paint for a couch/support SHELL, or None.

    Air cores return None here on purpose: pass 1 skips them and pass 2 paints
    them AIR. RayStation models the couch as Shell(2.03)/Qfix(1.20) walls with
    AIR cores, and QA compares MC against the TPS computation, so MC must use
    the same material model. (An earlier detour restored raw foam in the cores
    based on a COM 'overshoot' reading that turned out to be an artifact of
    Dose_Segmentation zeroing entry dose; gamma consistently ranked the
    walls+air construction best.)
    """
    m = material.lower().replace(" ", "")
    if "air" in m:
        return None                      # cores -> pass 2
    if "2.03" in m or "shell2.03" in m:
        return 8001                      # MedPhoton shell: 2.03 g/cm3, CFRP
    if "1.2" in m or "qfix" in m:
        return 8000                      # Qfix shell: 1.20 g/cm3, CFRP
    return None


# Named-material overrides. HU chosen so THIS scanner's density AND material
# curves both yield the intended values — see the table in the module docstring.
SDC_MATERIAL_HU = {
    "Gold":   8500,   # 19.2 g/cm3 + Au (73).   WAS 8040 -> Au at only 3.37 g/cm3
    "Water":  0,      # 1.00 g/cm3 + Water (17)
    "Muscle": 37,     # 1.05 g/cm3 + Schneider_SoftTissus (46)
    # "Iron": 8020,   # <- enable ONLY after adding 8020 to both scanner curves
    # "Titanium":     # <- not supportable: no titanium material in MCsquare
}


# ---------------------------------------------------------------------------
# Couch wall thickness
# ---------------------------------------------------------------------------
# OPEN QUESTION — do not change without a measurement.
#
# The rasterized couch walls under-carry WET vs the TPS couch model, and this
# dilation compensates. The original calibration (plan 3 LP, gantry 155, 4 wall
# crossings) measured: 0 vox -> 7.4 mm range overshoot, 1 vox -> 5.1 mm, and
# targeted 3 vox for ~0.
#
# It was set to 3, then changed to 0 on 2026-07-06 "to troubleshoot
# under-ranging" (APH) and not reverted.
#
# BUT: the pass-2 note records that those original overshoot readings were later
# found to be an artifact of Dose_Segmentation zeroing entry dose — so the 3-vox
# calibration may itself rest on a bad measurement. Both values are therefore
# suspect. Resolve by re-measuring range agreement on a posterior beam that
# crosses the couch (e.g. plan 15 field RP) at 0 / 1 / 3 vox, with the current
# Dose_Segmentation setting, and set it from that result.
COUCH_WALL_DILATION_VOX = 0


def _structure_material_overrides(rtstruct_path: str) -> dict[str, str]:
    """
    Return {ROIName: material_name} for every ROI carrying a material-override
    tag (0x300a, 0x00e1), matching how the SDC finds overrides. Only these are
    candidates; whether each is actually applied depends on SDC_MATERIAL_HU.
    """
    dcm = pydicom.dcmread(rtstruct_path, force=True)
    names = {s.ROINumber: str(s.ROIName) for s in dcm.StructureSetROISequence}
    overrides: dict[str, str] = {}
    for obs in getattr(dcm, "RTROIObservationsSequence", []):
        name = names.get(getattr(obs, "ReferencedROINumber", None))
        if name is None:
            continue
        try:
            material = str(obs[0x300a, 0x00e1].value)  # ROI material override
        except KeyError:
            continue
        overrides[name] = material
    return overrides


def _hu_for_material(material: str):
    """Resolve the HU to paint for a given RayStation material name, or None."""
    hu = _couch_hu_for_material(material)
    if hu is not None:
        return hu
    return SDC_MATERIAL_HU.get(material)


def apply_density_overrides(CT, rtstruct_path: str, hu_density_file: str = None,
                            log=print, unhandled_out: list = None) -> int:
    """
    Edit CT.Image in place to replicate RayStation's dose-calculation geometry:

      pass 0 — everything OUTSIDE the patient External -> AIR.
               The CT contains the SIMULATOR's couch (imaged with the patient);
               RayStation ignores everything outside External except Support
               structures, so the imaged sim couch must be erased or MCsquare
               transports through material the TPS never models (this was the
               root cause of LP's range undershoot).
      pass 1 — named materials (Gold/Water/Muscle) and Support shells painted
               solid at their reserved codes (8001 = MedPhoton 2.03,
               8000 = Qfix 1.20).
      pass 2 — Support cores painted AIR, carving the shells into walls.

    Net result: MCsquare sees exactly what RayStation computes through —
    patient + treatment-couch model, nothing else.

    Args:
        unhandled_out: optional list. Any material override that could not be
            resolved is appended as (roi_name, material). The caller SHOULD
            treat a non-empty list as a reason to force the plan's verdict to
            'measure': those ROIs were computed on raw CT while the TPS computed
            them as the named material, so the gamma is not a valid check.

    Returns the number of structures/regions applied.
    """
    overrides = _structure_material_overrides(rtstruct_path)
    if not overrides:
        log("no material overrides found in RTSTRUCT")
        return 0

    dcm = pydicom.dcmread(rtstruct_path, force=True)
    roi_by_number = {s.ROINumber: str(s.ROIName) for s in dcm.StructureSetROISequence}
    contour_by_name = {}
    for rc in getattr(dcm, "ROIContourSequence", []):
        nm = roi_by_number.get(getattr(rc, "ReferencedROINumber", None))
        if nm is not None:
            contour_by_name[nm] = rc

    nx, ny, nz = CT.GridSize[0], CT.GridSize[1], CT.GridSize[2]
    ipp = CT.ImagePositionPatient
    ps = CT.PixelSpacing

    def rasterize(rc):
        mask = np.zeros((nx, ny, nz), dtype=bool)
        for dslice in rc.ContourSequence:
            cd = dslice.ContourData
            xs = (np.asarray(cd[0::3], dtype=float) - ipp[0]) / ps[0]
            ys = (np.asarray(cd[1::3], dtype=float) - ipp[1]) / ps[1]
            sid = int(round((float(cd[2]) - ipp[2]) / ps[2]))
            if sid < 0 or sid >= nz:
                continue
            xy = list(zip(xs, ys))
            if len(xy) < 3:
                continue
            img = Image.new("L", (nx, ny), 0)
            ImageDraw.Draw(img).polygon(xy, outline=1, fill=1)
            mask[:, :, sid] |= np.array(img, dtype=bool)
        return mask

    applied = 0
    unhandled: list = []

    # ---- pass 0: everything OUTSIDE External -> AIR --------------------------
    # Replicates RayStation's dose-calc rule: material outside the patient
    # External is ignored (air), EXCEPT Support structures. Critically, the CT
    # contains the *simulator's* couch (imaged with the patient); the treatment
    # couch is modelled by the MedPhoton/Qfix Support contours. RayStation never
    # transports through the imaged sim couch — MCsquare must not either.
    ext_number = None
    for obs in getattr(dcm, "RTROIObservationsSequence", []):
        if str(getattr(obs, "RTROIInterpretedType", "")).upper() == "EXTERNAL":
            ext_number = getattr(obs, "ReferencedROINumber", None)
            break
    if ext_number is None:
        for num, nm in roi_by_number.items():
            if nm.strip().lower() == "external":
                ext_number = num
                break
    if ext_number is not None:
        ext_name = roi_by_number.get(ext_number, "External")
        rc_ext = next((r for r in getattr(dcm, "ROIContourSequence", [])
                       if getattr(r, "ReferencedROINumber", None) == ext_number), None)
        if rc_ext is not None and hasattr(rc_ext, "ContourSequence"):
            ext_mask = rasterize(rc_ext)
            n_out = int((~ext_mask).sum())
            CT.Image[~ext_mask] = np.float32(AIR_HU)
            log(f"external '{ext_name}': set {n_out} voxels outside patient to "
                f"AIR (erases imaged SIM couch — matches TPS calc rule)")
            applied += 1
        else:
            log("WARNING: External ROI has no contours — sim couch NOT erased")
    else:
        log("WARNING: no External ROI found — sim couch NOT erased")

    # ---- pass 1: named materials + shells painted solid ----------------------
    for name, material in overrides.items():
        m = material.lower().replace(" ", "")
        if "air" in m:
            continue  # cores handled in pass 2
        target_hu = _hu_for_material(material)
        if target_hu is None:
            unhandled.append((name, material))
            log("*" * 72)
            log(f"*** UNHANDLED MATERIAL OVERRIDE: '{material}' on ROI '{name}'")
            log(f"*** This ROI is being computed on RAW CT, but RayStation "
                f"computed it as '{material}'.")
            log(f"*** MCsquare and the TPS are therefore transporting through "
                f"DIFFERENT geometry;")
            log(f"*** the gamma for this plan is NOT a valid secondary check. "
                f"Plan should be MEASURED.")
            log("*" * 72)
            continue
        rc = contour_by_name.get(name)
        if rc is None or not hasattr(rc, "ContourSequence"):
            log(f"override '{name}': no contour data — skipped")
            continue
        mask = rasterize(rc)
        is_couch_shell = target_hu in (8000, 8001)
        if is_couch_shell and COUCH_WALL_DILATION_VOX > 0:
            from scipy.ndimage import binary_dilation
            struct = np.zeros((3, 3, 1), dtype=bool)
            struct[:, :, 0] = True   # in-plane (x,y) only; z untouched
            mask = binary_dilation(mask, structure=struct,
                                   iterations=COUCH_WALL_DILATION_VOX)
        n_vox = int(mask.sum())
        if n_vox == 0:
            log(f"override '{name}': rasterized to 0 voxels — skipped")
            continue
        CT.Image[mask] = np.float32(target_hu)
        dil = COUCH_WALL_DILATION_VOX if is_couch_shell else 0
        log(f"override '{name}': material='{material}' -> HU={int(target_hu)} "
            f"({n_vox} voxels, solid+{dil}vox dilation)")
        applied += 1

    # ---- pass 2: cores painted AIR — matching the TPS material model ---------
    # RayStation models the couch as Shell(2.03)/Qfix(1.20) walls with AIR
    # cores, and QA compares MC against the TPS computation, so MC must use the
    # same material model. The External-masked gamma excludes the segmented
    # couch voxels from scoring entirely.
    for name, material in overrides.items():
        m = material.lower().replace(" ", "")
        if "air" not in m:
            continue
        rc = contour_by_name.get(name)
        if rc is None or not hasattr(rc, "ContourSequence"):
            log(f"core '{name}': no contour data — skipped")
            continue
        mask = rasterize(rc)
        n_vox = int(mask.sum())
        if n_vox == 0:
            log(f"core '{name}': rasterized to 0 voxels — skipped")
            continue
        CT.Image[mask] = np.float32(AIR_HU)
        log(f"core '{name}': painted {n_vox} voxels AIR (HU={AIR_HU}) — "
            f"carves walls, matches TPS model")
        applied += 1

    # ---- summary -------------------------------------------------------------
    if unhandled:
        names = ", ".join(f"'{mat}' ({roi})" for roi, mat in unhandled)
        log(f"DENSITY OVERRIDE WARNING: {len(unhandled)} unhandled material(s) "
            f"left as raw CT: {names}. Secondary check is NOT valid for the "
            f"affected region — measure this plan.")
    if unhandled_out is not None:
        unhandled_out.extend(unhandled)

    return applied