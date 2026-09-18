import os
import numpy as np
import pydicom

from Process.RTplan import *

def export_plan_for_MCsquare(plan, file_path, CT, BDL):

  DestFolder, DestFile = os.path.split(file_path)
  FileName, FileExtension = os.path.splitext(DestFile)
    
  # Convert data for compatibility with MCsquare
  # These transformations may be modified in a future version  
  for beam in plan.Beams:
    beam.MCsquareIsocenter = []
    beam.MCsquareIsocenter.append(beam.IsocenterPosition[0] - CT.ImagePositionPatient[0] + CT.PixelSpacing[0]/2) # change coordinates (origin is now in the corner of the image)
    beam.MCsquareIsocenter.append(beam.IsocenterPosition[1] - CT.ImagePositionPatient[1] + CT.PixelSpacing[1]/2)
    beam.MCsquareIsocenter.append(beam.IsocenterPosition[2] - CT.ImagePositionPatient[2] + CT.PixelSpacing[2]/2)
    beam.MCsquareIsocenter[1] = CT.GridSize[1] * CT.PixelSpacing[1] - beam.MCsquareIsocenter[1] # flip coordinates in Y direction
   
  # Estimate number of delivered protons from MU
  plan.DeliveredProtons = 0
  plan.BeamletRescaling = []
  for beam in plan.Beams:
    for layer in beam.Layers:
      if BDL.isLoaded:
        DeliveredProtons = np.interp(layer.NominalBeamEnergy, BDL.NominalEnergy, BDL.ProtonsMU)
      else:
        DeliveredProtons += BDL.MU_to_NumProtons(1.0, layer.NominalBeamEnergy)
      plan.DeliveredProtons += sum(layer.SpotMU) * DeliveredProtons
      for spot in range(len(layer.SpotMU)):
        plan.BeamletRescaling.append(DeliveredProtons * 1.602176e-19 * 1000)
  
  # export plan      
  print("Write Plan: " + file_path)
  fid = open(file_path,'w');
  fid.write("#TREATMENT-PLAN-DESCRIPTION\n") 
  fid.write("#PlanName\n") 
  fid.write("%s\n" % FileName) 
  fid.write("#NumberOfFractions\n") 
  fid.write("%d\n" % plan.NumberOfFractionsPlanned) 
  fid.write("##FractionID\n") 
  fid.write("1\n")
  fid.write("##NumberOfFields\n") 
  fid.write("%d\n" % len(plan.Beams)) 
  for i in range(len(plan.Beams)):
    fid.write("###FieldsID\n") 
    fid.write("%d\n" % (i+1)) 
  fid.write("#TotalMetersetWeightOfAllFields\n") 
  fid.write("%f\n" % plan.TotalMeterset) 
  
  for i in range(len(plan.Beams)):
    fid.write("\n")
    fid.write("#FIELD-DESCRIPTION\n")
    fid.write("###FieldID\n")
    fid.write("%d\n" % (i+1)) 
    fid.write("###FinalCumulativeMeterSetWeight\n")
    fid.write("%f\n" % plan.Beams[i].BeamMeterset)
    fid.write("###GantryAngle\n")
    fid.write("%f\n" % plan.Beams[i].GantryAngle)
    fid.write("###PatientSupportAngle\n")
    fid.write("%f\n" % plan.Beams[i].PatientSupportAngle)
    fid.write("###IsocenterPosition\n")
    fid.write("%f\t %f\t %f\n" % tuple(plan.Beams[i].MCsquareIsocenter))
    
    RangeShifter = -1
    if plan.Beams[i].RangeShifterType == "binary":
      RangeShifter = next((RS for RS in BDL.RangeShifters if RS.ID == plan.Beams[i].RangeShifterID), -1)
      if(RangeShifter == -1):
        print("WARNING: Range shifter " + plan.Beams[i].RangeShifterID + " was not found in the BDL.")
      else:
        fid.write("###RangeShifterID\n")
        fid.write("%s\n" % RangeShifter.ID) 
        fid.write("###RangeShifterType\n")
        fid.write("binary\n")
      
    fid.write("###NumberOfControlPoints\n")
    fid.write("%d\n" % len(plan.Beams[i].Layers)) 
    fid.write("\n")
    fid.write("#SPOTS-DESCRIPTION\n")
    
    for j in range(len(plan.Beams[i].Layers)):
      fid.write("####ControlPointIndex\n")
      fid.write("%d\n" % (j+1))
      fid.write("####SpotTunnedID\n")
      fid.write("1\n")
      fid.write("####CumulativeMetersetWeight\n")
      fid.write("%f\n" % plan.Beams[i].Layers[j].CumulativeMeterset)
      fid.write("####Energy (MeV)\n")
      fid.write("%f\n" % plan.Beams[i].Layers[j].NominalBeamEnergy)
      
      if(RangeShifter != -1 and plan.Beams[i].RangeShifterType == "binary"):
        fid.write("####RangeShifterSetting\n")
        fid.write("%s\n" % plan.Beams[i].Layers[j].RangeShifterSetting)
        fid.write("####IsocenterToRangeShifterDistance\n")
        fid.write("%f\n" % plan.Beams[i].Layers[j].IsocenterToRangeShifterDistance)
        fid.write("####RangeShifterWaterEquivalentThickness\n")
        if(plan.Beams[i].Layers[j].RangeShifterWaterEquivalentThickness == ""):
          fid.write("%f\n" % RangeShifter.WET)
        else:
          fid.write("%f\n" % plan.Beams[i].Layers[j].RangeShifterWaterEquivalentThickness)
        
      fid.write("####NbOfScannedSpots\n")
      fid.write("%d\n" % len(plan.Beams[i].Layers[j].SpotMU))
      
      if hasattr(plan.Beams[i].Layers[j], 'SpotTiming'):
        fid.write("####X Y Weight Time\n")
        for k in range(len(plan.Beams[i].Layers[j].SpotMU)):
          fid.write("%f %f %f %f\n" % (plan.Beams[i].Layers[j].ScanSpotPositionMap_x[k], plan.Beams[i].Layers[j].ScanSpotPositionMap_y[k], plan.Beams[i].Layers[j].SpotMU[k], plan.Beams[i].Layers[j].SpotTiming[k]))
      else:
        fid.write("####X Y Weight\n")
        for k in range(len(plan.Beams[i].Layers[j].SpotMU)):
          fid.write("%f %f %f\n" % (plan.Beams[i].Layers[j].ScanSpotPositionMap_x[k], plan.Beams[i].Layers[j].ScanSpotPositionMap_y[k], plan.Beams[i].Layers[j].SpotMU[k]))
      
  fid.close()
  
  
