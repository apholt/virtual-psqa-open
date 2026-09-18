import pydicom
import numpy as np
import math
import time
import pickle


class RTplan:

  def __init__(self):
    self.SeriesInstanceUID = ""
    self.SOPInstanceUID = ""
    self.PatientInfo = {}
    self.StudyInfo = {}
    self.DcmFile = ""
    self.Modality = ""
    self.RadiationType = ""
    self.ScanMode = ""
    self.TreatmentMachineName = ""
    self.NumberOfFractionsPlanned = 1
    self.NumberOfSpots = 0
    self.Beams = []
    self.TotalMeterset = 0.0
    self.PlanName = ""
    self.isLoaded = 0
    self.beamlets = []
    self.OriginalDicomDataset = []
    
    
    
  def print_plan_info(self, prefix=""):
    print(prefix + "Plan: " + self.SeriesInstanceUID)
    print(prefix + "   " + self.DcmFile)
    
    
  
  def import_Dicom_plan(self):
    if(self.isLoaded == 1):
      print("Warning: RTplan " + self.SeriesInstanceUID + " is already loaded")
      return
      
    dcm = pydicom.dcmread(self.DcmFile)
    
    self.OriginalDicomDataset = dcm
    
    # Photon plan
    if dcm.SOPClassUID == "1.2.840.10008.5.1.4.1.1.481.5": 
      print("ERROR: Conventional radiotherapy (photon) plans are not supported")
      self.Modality = "Radiotherapy"
      return
  
    # Ion plan  
    elif dcm.SOPClassUID == "1.2.840.10008.5.1.4.1.1.481.8":
      self.Modality = "Ion therapy"
    
      if dcm.IonBeamSequence[0].RadiationType == "PROTON":
        self.RadiationType = "Proton"
      else:
        print("ERROR: Radiation type " + dcm.IonBeamSequence[0].RadiationType + " not supported")
        self.RadiationType = dcm.IonBeamSequence[0].RadiationType
        return
       
      if dcm.IonBeamSequence[0].ScanMode == "MODULATED":
        self.ScanMode = "MODULATED" # PBS
      else:
        print("ERROR: Scan mode " + dcm.IonBeamSequence[0].ScanMode + " not supported")
        self.ScanMode = dcm.IonBeamSequence[0].ScanMode
        return 
    
    # Other  
    else:
      print("ERROR: Unknown SOPClassUID " + dcm.SOPClassUID + " for file " + self.DcmFile)
      self.Modality = "Unknown"
      return
      
    # Start parsing PBS plan
    self.SOPInstanceUID = dcm.SOPInstanceUID
    self.NumberOfFractionsPlanned = int(dcm.FractionGroupSequence[0].NumberOfFractionsPlanned)
    self.NumberOfSpots = 0
    self.TotalMeterset = 0  
    
    if(hasattr(dcm.IonBeamSequence[0], 'TreatmentMachineName')):
      self.TreatmentMachineName = dcm.IonBeamSequence[0].TreatmentMachineName
    else:
      self.TreatmentMachineName = ""
  
    for dcm_beam in dcm.IonBeamSequence:
      if getattr(dcm_beam, "TreatmentDeliveryType", "TREATMENT") != "TREATMENT":
        continue

      
      first_layer = dcm_beam.IonControlPointSequence[0]
      
      beam = Plan_IonBeam()
      beam.SeriesInstanceUID = self.SeriesInstanceUID
      beam.BeamName = dcm_beam.BeamName
      beam.IsocenterPosition = [float(first_layer.IsocenterPosition[0]), float(first_layer.IsocenterPosition[1]), float(first_layer.IsocenterPosition[2])]
      beam.GantryAngle = float(first_layer.GantryAngle)
      beam.PatientSupportAngle = float(first_layer.PatientSupportAngle)
      beam.FinalCumulativeMetersetWeight = float(dcm_beam.FinalCumulativeMetersetWeight)
    
      # find corresponding beam in FractionGroupSequence (beam order may be different from IonBeamSequence)
      fg_seq = getattr(dcm, "FractionGroupSequence", [])
      ref_beams = getattr(fg_seq[0], "ReferencedBeamSequence", []) if fg_seq else []
      ReferencedBeam_id = next((x for x, val in enumerate(ref_beams) if getattr(val, "ReferencedBeamNumber", 0) == dcm_beam.BeamNumber), -1)
      if ReferencedBeam_id == -1:
        beam.BeamMeterset = float(getattr(dcm_beam, "FinalCumulativeMetersetWeight", 100.0) or 100.0)
      else:
        beam.BeamMeterset = float(getattr(ref_beams[ReferencedBeam_id], "BeamMeterset", 100.0) or 100.0)

    
      self.TotalMeterset += beam.BeamMeterset
    
      num_rs = getattr(dcm_beam, "NumberOfRangeShifters", 0)
      if num_rs == 0:
        beam.RangeShifterID = ""
        beam.RangeShifterType = "none"
      elif num_rs == 1:
        beam.RangeShifterID = getattr(dcm_beam.RangeShifterSequence[0], "RangeShifterID", "")
        rstype = getattr(dcm_beam.RangeShifterSequence[0], "RangeShifterType", "")
        if rstype == "BINARY":
          beam.RangeShifterType = "binary"
        elif rstype == "ANALOG":
          beam.RangeShifterType = "analog"

        else:
          print("ERROR: Unknown range shifter type for beam " + dcm_beam.BeamName)
          beam.RangeShifterType = "none"
      else: 
        print("ERROR: More than one range shifter defined for beam " + dcm_beam.BeamName)
        beam.RangeShifterID = ""
        beam.RangeShifterType = "none"
      
      
      SnoutPosition = 0
      if hasattr(first_layer, 'SnoutPosition'):
        SnoutPosition = float(first_layer.SnoutPosition)
    
      IsocenterToRangeShifterDistance = SnoutPosition
      RangeShifterWaterEquivalentThickness = ""
      RangeShifterSetting = "OUT"
      ReferencedRangeShifterNumber = 0
    
      if hasattr(first_layer, 'RangeShifterSettingsSequence'):
        if hasattr(first_layer.RangeShifterSettingsSequence[0], 'IsocenterToRangeShifterDistance'):
          IsocenterToRangeShifterDistance = float(first_layer.RangeShifterSettingsSequence[0].IsocenterToRangeShifterDistance)
        if hasattr(first_layer.RangeShifterSettingsSequence[0], 'RangeShifterWaterEquivalentThickness'):
          RangeShifterWaterEquivalentThickness = float(first_layer.RangeShifterSettingsSequence[0].RangeShifterWaterEquivalentThickness)
        if hasattr(first_layer.RangeShifterSettingsSequence[0], 'RangeShifterSetting'):
          RangeShifterSetting = first_layer.RangeShifterSettingsSequence[0].RangeShifterSetting
        if hasattr(first_layer.RangeShifterSettingsSequence[0], 'ReferencedRangeShifterNumber'):
          ReferencedRangeShifterNumber = int(first_layer.RangeShifterSettingsSequence[0].ReferencedRangeShifterNumber)
       
      CumulativeMeterset = 0
    
      for dcm_layer in dcm_beam.IonControlPointSequence:
        n_spots = getattr(dcm_layer, "NumberOfScanSpotPositions", 0) or 0
        if n_spots == 0:
          continue
        if n_spots == 1:
          weights = getattr(dcm_layer, "ScanSpotMetersetWeights", [0.0])
          sum_weights = float(weights[0] if isinstance(weights, (list, tuple)) else weights)
        else:
          sum_weights = sum(getattr(dcm_layer, "ScanSpotMetersetWeights", []))
      
        if sum_weights == 0.0:
          continue

        
        layer = Plan_IonLayer()
        layer.SeriesInstanceUID = self.SeriesInstanceUID
            
        if hasattr(dcm_layer, 'SnoutPosition'):
          SnoutPosition = float(dcm_layer.SnoutPosition)
        
        if hasattr(dcm_layer, 'NumberOfPaintings'): layer.NumberOfPaintings = int(dcm_layer.NumberOfPaintings)
        else: layer.NumberOfPaintings = 1
       
        layer.NominalBeamEnergy = float(dcm_layer.NominalBeamEnergy)
        layer.ScanSpotPositionMap_x = dcm_layer.ScanSpotPositionMap[0::2]
        layer.ScanSpotPositionMap_y = dcm_layer.ScanSpotPositionMap[1::2]
        layer.ScanSpotMetersetWeights = dcm_layer.ScanSpotMetersetWeights
        layer.SpotMU = np.array(dcm_layer.ScanSpotMetersetWeights) * beam.BeamMeterset / beam.FinalCumulativeMetersetWeight # spot weights are converted to MU
        if layer.SpotMU.size == 1: layer.SpotMU = [layer.SpotMU]
        else: layer.SpotMU = layer.SpotMU.tolist()
      
        self.NumberOfSpots += len(layer.SpotMU)
        CumulativeMeterset += sum(layer.SpotMU)
        layer.CumulativeMeterset = CumulativeMeterset
            
        if beam.RangeShifterType != "none":        
          if hasattr(dcm_layer, 'RangeShifterSettingsSequence'):
            RangeShifterSetting = dcm_layer.RangeShifterSettingsSequence[0].RangeShifterSetting
            ReferencedRangeShifterNumber = dcm_layer.RangeShifterSettingsSequence[0].ReferencedRangeShifterNumber
            if hasattr(dcm_layer.RangeShifterSettingsSequence[0], 'IsocenterToRangeShifterDistance'):
              IsocenterToRangeShifterDistance = dcm_layer.RangeShifterSettingsSequence[0].IsocenterToRangeShifterDistance
            if hasattr(dcm_layer.RangeShifterSettingsSequence[0], 'RangeShifterWaterEquivalentThickness'):
              RangeShifterWaterEquivalentThickness = dcm_layer.RangeShifterSettingsSequence[0].RangeShifterWaterEquivalentThickness
        
          layer.RangeShifterSetting = RangeShifterSetting
          layer.IsocenterToRangeShifterDistance = IsocenterToRangeShifterDistance
          layer.RangeShifterWaterEquivalentThickness = RangeShifterWaterEquivalentThickness
          layer.ReferencedRangeShifterNumber = ReferencedRangeShifterNumber
        
        
        beam.Layers.append(layer)
      
      self.Beams.append(beam)
      
    self.isLoaded = 1



  def export_Dicom_with_new_UID(self, OutputFile):
    # generate new uid
    initial_uid = self.OriginalDicomDataset.SOPInstanceUID
    new_uid = pydicom.uid.generate_uid()
    self.OriginalDicomDataset.SOPInstanceUID = new_uid

    # save dicom file
    print("Export dicom RTPLAN: " + OutputFile)
    self.OriginalDicomDataset.save_as(OutputFile)

    # restore initial uid
    self.OriginalDicomDataset.SOPInstanceUID = initial_uid

    return new_uid



  def save(self, file_path):
    beamlets = self.beamlets
    self.beamlets = []
    dcm = self.OriginalDicomDataset
    self.OriginalDicomDataset = []

    with open(file_path, 'wb') as fid:
      pickle.dump(self.__dict__, fid)

    self.beamlets = beamlets
    self.OriginalDicomDataset = dcm



  def load(self, file_path):
    with open(file_path, 'rb') as fid:
      tmp = pickle.load(fid)

    self.__dict__.update(tmp) 
  
    
  
      
class Plan_IonBeam:

  def __init__(self):
    self.SeriesInstanceUID = ""
    self.BeamName = ""
    self.IsocenterPosition = [0,0,0]
    self.GantryAngle = 0.0
    self.PatientSupportAngle = 0.0
    self.FinalCumulativeMetersetWeight = 0.0
    self.BeamMeterset = 0.0
    self.RangeShifter = "none"
    self.Layers = []
    
    
    
class Plan_IonLayer:

  def __init__(self):
    self.SeriesInstanceUID = ""
    self.NumberOfPaintings = 1
    self.NominalBeamEnergy = 0.0
    self.ScanSpotPositionMap_x = []
    self.ScanSpotPositionMap_y = []
    self.ScanSpotMetersetWeights = []
    self.SpotMU = []
    self.CumulativeMeterset = 0.0
    self.RangeShifterSetting = 'OUT'
    self.IsocenterToRangeShifterDistance = 0.0
    self.RangeShifterWaterEquivalentThickness = 0.0
    self.ReferencedRangeShifterNumber = 0
    
    
    
