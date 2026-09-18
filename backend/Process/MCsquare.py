import os
import shutil
import numpy as np
import scipy.sparse as sp
import subprocess
import platform

from Process.MHD_image import *
from Process.MCsquare_BDL import *
from Process.MCsquare_CT_calibration import *
from Process.MCsquare_plan import *
from Process.MCsquare_config import *
from Process.RTdose import *

class MCsquare:

  def __init__(self):
    self.Path_MCsquareLib = os.path.abspath("./MCsquare")
    self.WorkDir = os.path.join(os.path.expanduser('~'), "Work");
    self.DoseName = "MCsquare_dose"
    self.BDL = MCsquare_BDL()
    self.Scanner = MCsquare_CT_calibration()
    self.NumProtons = 1e7
    self.MaxUncertainty = 2.0
    self.dose2water = 1
    self.config = {}
    self.Crop_CT_contour = {}
    self.Compute_DVH_only = 0
    


  def MCsquare_version(self):
    if(platform.system() == "Linux"): os.system(os.path.join(self.Path_MCsquareLib, "MCsquare_linux") + " -v")
    elif(platform.system() == "Windows"): os.system(os.path.join(self.Path_MCsquareLib, "MCsquare_win.exe") + " -v")
    else: print("Error: not compatible with " + platform.system() + " system.")

  
  
  def MCsquare_simulation(self, CT, Plan,AnalyzeIndividualFields = False):
    print("Prepare MCsquare simulation")
  
    self.init_simulation_directory()
      
    # Export CT image
    self.export_CT_for_MCsquare(CT, os.path.join(self.WorkDir, "CT.mhd"), self.Crop_CT_contour)
    
    # Export treatment plan
    self.BDL.import_BDL()
    export_plan_for_MCsquare(Plan, os.path.join(self.WorkDir, "PlanPencil.txt"), CT, self.BDL)
    
    # Generate MCsquare configuration file
    print(AnalyzeIndividualFields)
    self.config = generate_MCsquare_config(self.WorkDir, self.NumProtons, self.Scanner.get_path(), self.BDL.get_path(), 'CT.mhd', 'PlanPencil.txt',AnalyzeIndividualFields)
    #if(self.dose2water > 0): self.config["Dose_to_Water_conversion"] = "OnlineSPR"
    #else: self.config["Dose_to_Water_conversion"] = "Disabled"
    self.config["Stat_uncertainty"] = self.MaxUncertainty
    if(self.Compute_DVH_only > 0):
      self.config["Dose_MHD_Output"] = False
      self.config["Compute_DVH"] = True
    export_MCsquare_config(self.config)
    
    # Start simulation
    print("\nStart MCsquare simulation")
    if(platform.system() == "Linux"): os.system("cd " + self.WorkDir + " && " + os.path.join(self.Path_MCsquareLib, "MCsquare"))
    elif(platform.system() == "Windows"): os.system("cd " + self.WorkDir + " && " + os.path.join(self.Path_MCsquareLib, "MCsquare_win.bat"))
    else: print("Error: not compatible with " + platform.system() + " system.")
    
    # Import dose result
    mhd_dose = self.import_MCsquare_dose(Plan)
    
    return mhd_dose
    
    
    
  def import_MCsquare_dose(self, plan, FileName="Dose.mhd", DoseScaling=0.9):
  
    dose_file = os.path.join(self.WorkDir, "Outputs", FileName)
  
    if not os.path.isfile(dose_file):
      print("ERROR: file " + dose_file + " not found!")
      return None
    else: print("Read dose file: " + dose_file)
  
    mhd_image = MHD_image()
    mhd_image.import_MHD_header(dose_file)
    mhd_image.import_MHD_data()
  
    # Convert data for compatibility with MCsquare
    # These transformations may be modified in a future version
    mhd_image.Image = np.flip(mhd_image.Image, 0)
    mhd_image.Image = np.flip(mhd_image.Image, 1)
  
    # Convert in Gray units
    mhd_image.Image = mhd_image.Image * 1.602176e-19 * 1000 * plan.DeliveredProtons * plan.NumberOfFractionsPlanned * DoseScaling;
  
    return mhd_image
    
    
    
  def export_CT_for_MCsquare(self, CT, file_path, Crop_CT_contour):
  
    if CT.GridSize[0] != CT.GridSize[1]:
      print("WARNING: different number of voxels in X and Y directions may not be fully supported")  

    mhd_image = CT.convert_to_MHD()
    
    # Crop CT image with contour
    if(Crop_CT_contour != {}):
      mhd_image.Image[Crop_CT_contour.Mask == False] = -1024

  
    # Convert data for compatibility with MCsquare
    # These transformations may be modified in a future version
    mhd_image.Image = np.flip(mhd_image.Image, 0)
    mhd_image.Image = np.flip(mhd_image.Image, 1)

    # export image
    mhd_image.export_MHD_header(file_path)
    mhd_image.export_MHD_data()
  
  
  
  def export_contour_for_MCsquare(self, Contour, folder_path):

    if not os.path.isdir(folder_path):
      os.mkdir(folder_path)
    
    mhd_image = Contour.convert_to_MHD()
  
    # Convert data for compatibility with MCsquare
    # These transformations may be modified in a future version
    mhd_image.Image = np.flip(mhd_image.Image, 0)
    mhd_image.Image = np.flip(mhd_image.Image, 1)
    
    # generate output path
    ContourName = Contour.ROIName.replace(' ', '_').replace('-', '_').replace('.', '_').replace('/', '_')
    file_path = os.path.join(folder_path, ContourName + ".mhd")
  
    # export image
    mhd_image.export_MHD_header(file_path)
    mhd_image.export_MHD_data()
    
    


  def get_simulation_progress(self):
    progression_file = os.path.join(self.WorkDir, "Outputs", "Simulation_progress.txt")

    simulation_started = 0
    batch = 0
    uncertainty = -1
    multiplier = 1.0

    with open(progression_file, 'r') as fid:
      for line in fid:
        if "Simulation started (" in line:
          simulation_started = 0
          batch = 0
          uncertainty = -1
          multiplier = 1.0

        elif "batch " in line and " completed" in line:
          tmp = line.split(' ')
          if(tmp[1].isnumeric()): batch = int(tmp[1])
          if(len(tmp) >= 6): uncertainty = float(tmp[5])

        elif "10x more particles per batch" in line:
          multiplier *= 10.0

    NumParticles = int(batch * multiplier * self.NumProtons / 10.0)
    return NumParticles, uncertainty



  def init_simulation_directory(self):
    # Create simulation directory
    if not os.path.isdir(self.WorkDir):
      os.mkdir(self.WorkDir)

    # Clean structs directory
    struct_dir = os.path.join(self.WorkDir, "structs")
    if os.path.isdir(struct_dir):
      shutil.rmtree(struct_dir)

    # Clean output directory
    out_dir = os.path.join(self.WorkDir, "Outputs")
    if os.path.isdir(out_dir):
      file_list = os.listdir(out_dir)
      for file in file_list:

        if(file.endswith(".mhd")): os.remove(os.path.join(out_dir, file))
        if(file.endswith(".raw")): os.remove(os.path.join(out_dir, file))
        if(file.endswith(".txt")): os.remove(os.path.join(out_dir, file))
        if(file.endswith(".bin")): os.remove(os.path.join(out_dir, file))

        if(file == "tmp" and os.path.isdir(os.path.join(out_dir, file))):
          folder_path = os.path.join(self.WorkDir, "Outputs", "tmp")
          for root, dirs, files in os.walk(folder_path, topdown=False):
            for name in files: os.remove(os.path.join(root, name))
            for name in dirs: os.rmdir(os.path.join(root, name))




  def Copy_BDL_to_WorkDir(self, Output_FileName='BDL.txt'):
    source_path = self.BDL.get_path()
    destination_path = os.path.join(self.WorkDir, Output_FileName)
    shutil.copyfile(source_path, destination_path)



  def Copy_CT_calib_to_WorkDir(self, Density_FileName="HU_Density_Conversion.txt", Material_FileName="HU_Material_Conversion.txt"):
    source_path = os.path.join(self.Scanner.get_path(), "HU_Density_Conversion.txt")
    destination_path = os.path.join(self.WorkDir, Density_FileName)
    shutil.copyfile(source_path, destination_path)

    source_path = os.path.join(self.Scanner.get_path(), "HU_Material_Conversion.txt")
    destination_path = os.path.join(self.WorkDir, Material_FileName)
    shutil.copyfile(source_path, destination_path)



  def Copy_Materials_to_WorkDir(self):
    source_path = os.path.join(self.Path_MCsquareLib, "Materials")
    destination_path = os.path.join(self.WorkDir, "Materials")
    if(not os.path.isdir(destination_path)): shutil.copytree(source_path, destination_path)



  def Copy_MCsquare_bin_to_WorkDir(self, OperatingSystem="linux"):
    if(OperatingSystem == "linux"):
      source_path = os.path.join(self.Path_MCsquareLib, "MCsquare")
      destination_path = os.path.join(self.WorkDir, "MCsquare")
      shutil.copyfile(source_path, destination_path) # copy file
      shutil.copymode(source_path, destination_path) # copy permissions
      
      source_path = os.path.join(self.Path_MCsquareLib, "MCsquare_linux")
      destination_path = os.path.join(self.WorkDir, "MCsquare_linux")
      shutil.copyfile(source_path, destination_path)
      shutil.copymode(source_path, destination_path)
      
      source_path = os.path.join(self.Path_MCsquareLib, "MCsquare_linux_avx")
      destination_path = os.path.join(self.WorkDir, "MCsquare_linux_avx")
      shutil.copyfile(source_path, destination_path)
      shutil.copymode(source_path, destination_path)
      
      source_path = os.path.join(self.Path_MCsquareLib, "MCsquare_linux_avx2")
      destination_path = os.path.join(self.WorkDir, "MCsquare_linux_avx2")
      shutil.copyfile(source_path, destination_path)
      shutil.copymode(source_path, destination_path)
      
      source_path = os.path.join(self.Path_MCsquareLib, "MCsquare_linux_avx512")
      destination_path = os.path.join(self.WorkDir, "MCsquare_linux_avx512")
      shutil.copyfile(source_path, destination_path)
      shutil.copymode(source_path, destination_path)
      
      source_path = os.path.join(self.Path_MCsquareLib, "MCsquare_linux_sse4")
      destination_path = os.path.join(self.WorkDir, "MCsquare_linux_sse4")
      shutil.copyfile(source_path, destination_path)
      shutil.copymode(source_path, destination_path)

    elif(OperatingSystem == "windows"):
      source_path = os.path.join(self.Path_MCsquareLib, "MCsquare_win.bat")
      destination_path = os.path.join(self.WorkDir, "MCsquare_win.bat")
      shutil.copyfile(source_path, destination_path)
      shutil.copymode(source_path, destination_path)

      source_path = os.path.join(self.Path_MCsquareLib, "MCsquare_win.exe")
      destination_path = os.path.join(self.WorkDir, "MCsquare_win.exe")
      shutil.copyfile(source_path, destination_path)
      shutil.copymode(source_path, destination_path)

      source_path = os.path.join(self.Path_MCsquareLib, "libiomp5md.dll")
      destination_path = os.path.join(self.WorkDir, "libiomp5md.dll")
      shutil.copyfile(source_path, destination_path)
      shutil.copymode(source_path, destination_path)

    else:
      print("Error: Operating system " + OperatingSystem + " is not supported.")
