import pydicom
import numpy as np
#from matplotlib.path import Path
from PIL import Image, ImageDraw

from Process.MHD_image import *

class RTstruct:

  def __init__(self):
    self.SeriesInstanceUID = ""
    self.PatientInfo = {}
    self.StudyInfo = {}
    self.CT_SeriesInstanceUID = ""
    self.DcmFile = ""
    self.isLoaded = 0
    self.Contours = []
    self.NumContours = 0
    
    
  def print_struct_info(self, prefix=""):
    print(prefix + "Struct: " + self.SeriesInstanceUID)
    print(prefix + "   " + self.DcmFile)
    
    
  def print_ROINames(self):
    print("RT Struct UID: " + self.SeriesInstanceUID)
    count = -1
    for contour in self.Contours:
      count += 1
      print('  [' + str(count) + ']  ' + contour.ROIName)
    
  
  
  def import_Dicom_struct(self, CT):
    if(self.isLoaded == 1):
      print("Warning: RTstruct " + self.SeriesInstanceUID + " is already loaded")
      return
      
    dcm = pydicom.dcmread(self.DcmFile, force=True)
    
    self.CT_SeriesInstanceUID = CT.SeriesInstanceUID
    
    roi_contour_seq = getattr(dcm, "ROIContourSequence", [])
    for dcm_struct in getattr(dcm, "StructureSetROISequence", []):    
      roi_number = getattr(dcm_struct, "ROINumber", None)
      ReferencedROI_id = next((x for x, val in enumerate(roi_contour_seq) if getattr(val, "ReferencedROINumber", None) == roi_number), -1)
      if ReferencedROI_id == -1:
        continue
      dcm_contour = roi_contour_seq[ReferencedROI_id]

      if not hasattr(dcm_contour, 'ContourSequence') or not dcm_contour.ContourSequence:
        continue

      Contour = ROIcontour()
      Contour.SeriesInstanceUID = self.SeriesInstanceUID
      Contour.ROIName = getattr(dcm_struct, "ROIName", f"ROI_{roi_number}")

      raw_color = getattr(dcm_contour, "ROIDisplayColor", None)
      if raw_color is not None and len(raw_color) >= 3:
        try:
          Contour.ROIDisplayColor = [int(c) for c in raw_color[:3]]
        except Exception:
          Contour.ROIDisplayColor = [255, 0, 0]
      else:
        Contour.ROIDisplayColor = [255, 0, 0]
    
      Contour.Mask = np.zeros((CT.GridSize[0], CT.GridSize[1], CT.GridSize[2]), dtype=bool)
      Contour.Mask_GridSize = CT.GridSize
      Contour.Mask_PixelSpacing = CT.PixelSpacing
      Contour.Mask_Offset = CT.ImagePositionPatient
      Contour.Mask_NumVoxels = CT.NumVoxels   
      Contour.ContourMask = np.zeros((CT.GridSize[0], CT.GridSize[1], CT.GridSize[2]), dtype=bool)
      
      SOPInstanceUID_match = 1

      for dcm_slice in dcm_contour.ContourSequence:
        cdata = getattr(dcm_slice, "ContourData", None)
        if cdata is None or len(cdata) < 3:
          continue
        Slice = {}
      
        # list of Dicom coordinates
        Slice["XY_dcm"] = list(zip( np.array(cdata[0::3]), np.array(cdata[1::3]) ))
        Slice["Z_dcm"] = float(cdata[2])
      
        # list of coordinates in the image frame
        Slice["XY_img"] = list(zip( ((np.array(cdata[0::3]) - CT.ImagePositionPatient[0]) / CT.PixelSpacing[0]), ((np.array(cdata[1::3]) - CT.ImagePositionPatient[1]) / CT.PixelSpacing[1]) ))
        Slice["Z_img"] = (Slice["Z_dcm"] - CT.ImagePositionPatient[2]) / CT.PixelSpacing[2]
        Slice["Slice_id"] = int(round(Slice["Z_img"]))

        if Slice["Slice_id"] < 0 or Slice["Slice_id"] >= CT.GridSize[2]:
          continue
      
        # convert polygon to mask (based on PIL - fast)
        img = Image.new('L', (CT.GridSize[0], CT.GridSize[1]), 0)
        if(len(Slice["XY_img"]) > 1): ImageDraw.Draw(img).polygon(Slice["XY_img"], outline=1, fill=1)
        mask = np.array(img)
        Contour.Mask[:,:,Slice["Slice_id"]] = np.logical_or(Contour.Mask[:,:,Slice["Slice_id"]], mask)
        
        # do the same, but only keep contour in the mask
        img = Image.new('L', (CT.GridSize[0], CT.GridSize[1]), 0)
        if(len(Slice["XY_img"]) > 1): ImageDraw.Draw(img).polygon(Slice["XY_img"], outline=1, fill=0)
        mask = np.array(img)
        Contour.ContourMask[:,:,Slice["Slice_id"]] = np.logical_or(Contour.ContourMask[:,:,Slice["Slice_id"]], mask)
            
        Contour.ContourSequence.append(Slice)
      
        # check if the contour sequence is imported on the correct CT slice:
        if (
          hasattr(dcm_slice, 'ContourImageSequence')
          and dcm_slice.ContourImageSequence
          and hasattr(CT, 'SOPInstanceUIDs')
          and CT.SOPInstanceUIDs
          and Slice["Slice_id"] < len(CT.SOPInstanceUIDs)
          and hasattr(dcm_slice.ContourImageSequence[0], 'ReferencedSOPInstanceUID')
          and CT.SOPInstanceUIDs[Slice["Slice_id"]] != dcm_slice.ContourImageSequence[0].ReferencedSOPInstanceUID
        ):
          SOPInstanceUID_match = 0
      
      if SOPInstanceUID_match != 1:
        print("WARNING: some SOPInstanceUIDs don't match during importation of " + Contour.ROIName + " contour on CT image")
      
    
      self.Contours.append(Contour)
      self.NumContours += 1
      
    self.isLoaded = 1

      
      
class ROIcontour:

  def __init__(self):
    self.SeriesInstanceUID = ""
    self.ROIName = ""
    self.ContourSequence = []
    self.ROIDisplayColor = []
    self.Mask = []
    self.ContourMask = []
    self.Mask_GridSize = []
    self.Mask_PixelSpacing = []
    self.Mask_Offset = []
    self.Mask_NumVoxels = 0   
    
    
    
  def convert_to_MHD(self):
    mhd_image = MHD_image()
    mhd_image.ImagePositionPatient = self.Mask_Offset.copy()
    mhd_image.PixelSpacing = self.Mask_PixelSpacing.copy()
    mhd_image.GridSize = self.Mask_GridSize.copy()
    mhd_image.NumVoxels = self.Mask_NumVoxels
    mhd_image.Image = self.Mask.copy()
    
    return mhd_image
    
