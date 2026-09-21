import tempfile
from pathlib import Path
from unittest.mock import MagicMock
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence

from Process.RTstruct import RTstruct
from Process.DVH import DVH
from services.dvh_service import load_rois_from_rtstruct, DoseGrid
from services.oir_service import _get_parsed_rtstruct_contours


class MockCT:
    def __init__(self):
        self.SeriesInstanceUID = "1.2.840.10008.1.1"
        self.GridSize = [20, 20, 10]
        self.PixelSpacing = [2.0, 2.0, 3.0]
        self.ImagePositionPatient = [-20.0, -20.0, 0.0]
        self.NumVoxels = 20 * 20 * 10
        self.SOPInstanceUIDs = [f"sop.{i}" for i in range(10)]


def create_test_rtstruct_dicom(
    file_path: Path,
    omit_roi_display_color: bool = True,
    empty_roi_display_color: bool = False,
    include_orphan_roi: bool = True,
    include_empty_contour: bool = True,
):
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.481.3"
    file_meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.3680043.9.7243.rtstruct.1"
    file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian

    ds = pydicom.dataset.FileDataset(str(file_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.Modality = "RTSTRUCT"
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.481.3"
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.SeriesInstanceUID = "1.2.826.0.1.3680043.9.7243.structseries.1"

    # StructureSetROISequence
    roi1 = Dataset()
    roi1.ROINumber = 1
    roi1.ROIName = "Target_PTV"

    roi2 = Dataset()
    roi2.ROINumber = 2
    roi2.ROIName = "OAR_Bladder"

    roi_seq = [roi1, roi2]

    if include_orphan_roi:
        roi3 = Dataset()
        roi3.ROINumber = 3
        roi3.ROIName = "Orphan_ROI_No_Contour"
        roi_seq.append(roi3)

    if include_empty_contour:
        roi4 = Dataset()
        roi4.ROINumber = 4
        roi4.ROIName = "Empty_Contour_ROI"
        roi_seq.append(roi4)

    ds.StructureSetROISequence = Sequence(roi_seq)

    # RTROIObservationsSequence
    obs1 = Dataset()
    obs1.ObservationNumber = 1
    obs1.ReferencedROINumber = 1
    obs1.RTROIInterpretedType = "PTV"

    obs2 = Dataset()
    obs2.ObservationNumber = 2
    obs2.ReferencedROINumber = 2
    obs2.RTROIInterpretedType = "ORGAN"

    ds.RTROIObservationsSequence = Sequence([obs1, obs2])

    # ROIContourSequence
    c1 = Dataset()
    c1.ReferencedROINumber = 1
    if not omit_roi_display_color:
        c1.ROIDisplayColor = [255, 0, 0]
    elif empty_roi_display_color:
        c1.ROIDisplayColor = None
    # If omit_roi_display_color is True, ROIDisplayColor tag is completely omitted!

    # Contours for ROI 1
    s1 = Dataset()
    s1.ContourGeometricType = "CLOSED_PLANAR"
    s1.NumberOfContourPoints = 4
    s1.ContourData = [
        -5.0, -5.0, 3.0,
        5.0, -5.0, 3.0,
        5.0, 5.0, 3.0,
        -5.0, 5.0, 3.0,
    ]
    s1_img = Dataset()
    s1_img.ReferencedSOPInstanceUID = "sop.1"
    s1.ContourImageSequence = Sequence([s1_img])
    c1.ContourSequence = Sequence([s1])

    c2 = Dataset()
    c2.ReferencedROINumber = 2
    if not omit_roi_display_color:
        c2.ROIDisplayColor = [0, 255, 0]
    # Omit ROIDisplayColor completely for ROI 2

    s2 = Dataset()
    s2.ContourGeometricType = "CLOSED_PLANAR"
    s2.NumberOfContourPoints = 4
    s2.ContourData = [
        -2.0, -2.0, 6.0,
        2.0, -2.0, 6.0,
        2.0, 2.0, 6.0,
        -2.0, 2.0, 6.0,
    ]
    s2_img = Dataset()
    s2_img.ReferencedSOPInstanceUID = "sop.2"
    s2.ContourImageSequence = Sequence([s2_img])
    c2.ContourSequence = Sequence([s2])

    contour_seq = [c1, c2]

    if include_empty_contour:
        c4 = Dataset()
        c4.ReferencedROINumber = 4
        # Has no ContourSequence
        contour_seq.append(c4)

    ds.ROIContourSequence = Sequence(contour_seq)
    ds.save_as(str(file_path))
    return ds


def test_import_dicom_struct_without_roidisplaycolor():
    """Verify RTstruct.import_Dicom_struct succeeds when ROIDisplayColor is missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        rtstruct_path = Path(tmpdir) / "rtstruct_no_color.dcm"
        create_test_rtstruct_dicom(
            rtstruct_path,
            omit_roi_display_color=True,
            include_orphan_roi=True,
            include_empty_contour=True,
        )

        mock_ct = MockCT()
        rt = RTstruct()
        rt.SeriesInstanceUID = "1.2.826.0.1.3680043.9.7243.structseries.1"
        rt.DcmFile = str(rtstruct_path)

        # This previously raised AttributeError: 'Dataset' object has no attribute 'ROIDisplayColor'
        rt.import_Dicom_struct(mock_ct)

        assert rt.isLoaded == 1
        assert len(rt.Contours) == 2
        assert rt.Contours[0].ROIName == "Target_PTV"
        assert rt.Contours[0].ROIDisplayColor == [255, 0, 0]
        assert rt.Contours[0].Mask.shape == (20, 20, 10)
        assert rt.Contours[0].Mask.any()

        assert rt.Contours[1].ROIName == "OAR_Bladder"
        assert rt.Contours[1].ROIDisplayColor == [255, 0, 0]
        assert rt.Contours[1].Mask.any()

        # Check DVH initialization doesn't fail
        dvh = DVH(Contour=rt.Contours[0])
        assert dvh.ROIName == "Target_PTV"
        assert dvh.ROIDisplayColor == [255, 0, 0]


def test_import_dicom_struct_empty_color_and_orphans():
    """Verify RTstruct handles None ROIDisplayColor and unreferenced ROIs gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        rtstruct_path = Path(tmpdir) / "rtstruct_empty_color.dcm"
        create_test_rtstruct_dicom(
            rtstruct_path,
            omit_roi_display_color=True,
            empty_roi_display_color=True,
            include_orphan_roi=True,
            include_empty_contour=True,
        )

        mock_ct = MockCT()
        rt = RTstruct()
        rt.SeriesInstanceUID = "1.2.826.0.1.3680043.9.7243.structseries.1"
        rt.DcmFile = str(rtstruct_path)

        rt.import_Dicom_struct(mock_ct)

        assert rt.isLoaded == 1
        assert len(rt.Contours) == 2
        for contour in rt.Contours:
            assert isinstance(contour.ROIDisplayColor, list)
            assert len(contour.ROIDisplayColor) == 3


def test_dvh_service_load_rois_missing_color():
    """Verify load_rois_from_rtstruct in dvh_service handles missing ROIDisplayColor."""
    with tempfile.TemporaryDirectory() as tmpdir:
        rtstruct_path = Path(tmpdir) / "rtstruct_no_color.dcm"
        create_test_rtstruct_dicom(
            rtstruct_path,
            omit_roi_display_color=True,
            include_orphan_roi=True,
            include_empty_contour=True,
        )

        grid = DoseGrid(
            array=np.zeros((10, 20, 20), dtype=np.float32),
            origin=(0.0, -20.0, -20.0),
            spacing=(3.0, 2.0, 2.0),
        )
        rois = load_rois_from_rtstruct(rtstruct_path, grid)
        assert len(rois) >= 2
        for r in rois:
            assert "color" in r
            assert r["color"].startswith("#")


def test_oir_service_parsed_contours_missing_color():
    """Verify _get_parsed_rtstruct_contours handles missing ROIDisplayColor."""
    with tempfile.TemporaryDirectory() as tmpdir:
        rtstruct_path = Path(tmpdir) / "rtstruct_no_color.dcm"
        create_test_rtstruct_dicom(
            rtstruct_path,
            omit_roi_display_color=True,
            include_orphan_roi=True,
            include_empty_contour=True,
        )

        mock_plan = MagicMock()
        mock_plan.id = 9999

        # Monkeypatch find_plan_rtstruct
        from unittest.mock import patch
        with patch("services.oir_service.find_plan_rtstruct", return_value=rtstruct_path):
            parsed = _get_parsed_rtstruct_contours(9999, mock_plan)
            assert len(parsed) >= 2
            for z, num, name, color, pts in parsed:
                assert isinstance(color, list)
                assert len(color) == 3
