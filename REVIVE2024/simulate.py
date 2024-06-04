import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
# import datetime as dt
from datetime import datetime as dt
from datetime import timedelta
import email.utils as eutils
from statistics import mean
import eppy as eppy
from eppy import modeleditor
from eppy.modeleditor import IDF
from eppy.runner.run_functions import runIDFs
import multiprocessing as mp
import multiprocessing.pool as mp_pool
# from PIL import Image, ImageTk
import os
import gc
from eppy.results import readhtml # the eppy module with functions to read the html
from eppy.results import fasthtml
import subprocess
import csv
import os
import sys
from os import system
import json
import pylatex
import adorb
import envelope
import hvac
import internalHeatGains
import outputs
# import parallel
import renewables
import schedules
import simControl
import weatherMorph

"""Class to hold all inputs that do not vary from run to run""" 
class SimInputs:

    def __init__(self, batch_name, idd_file, study_folder, run_list, database_dir, num_procs, graphs_enabled, pdf_enabled, is_dummy_mode):
        # collect basic input information
        self.is_dummy_mode = is_dummy_mode
        self.idd_file = idd_file
        self.database_dir = database_dir
        self.num_procs = num_procs
        self.graphs_enabled = graphs_enabled
        self.pdf_enabled = pdf_enabled
        
        # establish dummy-dependent input information
        self.batch_name = batch_name if not is_dummy_mode else os.path.abspath("dummy")
        self.study_folder = study_folder if not is_dummy_mode else os.path.abspath("dummy")
        self.run_list = run_list if not is_dummy_mode else os.path.join(study_folder, "dummy_runlist.csv")

        # generate the implied input information
        self.generate_database_inputs()
        self.load_runlist()


    def generate_database_inputs(self):
        # emissions and weather data
        emissions_file = "Hourly Emission Rates.csv"
        weather_folder = "Weather Data"
        construction_file = "Construction Database.csv"
        self.emissions_db = os.path.join(self.database_dir, emissions_file)
        self.weather_db = os.path.join(self.database_dir, weather_folder)
        self.construction_db = os.path.join(self.database_dir, construction_file)


    def load_runlist(self):
        self.run_list_df = pd.read_csv(self.run_list)
        self.total_runs = self.run_list_df.shape[0]



class SimulationManager:
    def __init__(self, q, num_tasks, stop_event):
        # init the progress queue
        self.q = q

        # compute the increment amount
        self.num_checkpoints = 10 # changes based on how many baked into code
        self.increment_amt = 1 / (self.num_checkpoints * num_tasks)

        # init the stop event
        self.stop_event = stop_event

    
    def send_progress(self):
        if self.q is not None:
            self.q.put(self.increment_amt)

    def raise_exception(self, msg):
        if self.q is not None:
            try:
                self.q.put(msg)
            except BrokenPipeError:
                print(f"Failed to send message: \"{msg}\"")
    
    def check_if_stopped(self):
        return self.stop_event.is_set()
        


class GracefulExitException(Exception):
    """Throw specific type of error to signify graceful exit."""
    pass



### UTILITY FUNCTIONS

def validate_input(batch_name, idd_file, study_folder, run_list, db_dir):
    # ensure all fields are not empty
    try:
        assert batch_name, "a batch name"
        assert idd_file, "the location of the Energy+ IDD File"
        assert study_folder, "a study/output folder"
        assert run_list, "a run list file"
        assert db_dir, "the location of the database folder"
    except AssertionError as missing_item:
        return f"Please specify {missing_item}."
    
    # ensure all file paths can be found
    try:
        assert os.path.isfile(idd_file), f"Energy+ IDD file path ({idd_file})"
        assert os.path.isfile(run_list), f"Run list file path ({run_list})"
        assert os.path.isdir(study_folder), f"Study/output folder path ({study_folder})"
        assert os.path.isdir(db_dir), f"Database folder path ({db_dir})"
    except AssertionError as wrong_path:
        return f"{wrong_path} does not exist."
    
    # ensure database folder contains necessary files/folders
    try:
        emissions_file = "Hourly Emission Rates.csv"
        weather_folder = "Weather Data"
        construction_file = "Construction Database.csv"
        assert os.path.isfile(os.path.join(db_dir, emissions_file)), f"file \"{emissions_file}\""
        assert os.path.isdir(os.path.join(db_dir, weather_folder)), f"folder \"{weather_folder}\""
        assert os.path.isfile(os.path.join(db_dir, construction_file)), f"file \"{construction_file}\""
    except AssertionError as missing_item:
        return f"Cannot find {missing_item} in specified database directory."
    
    # ensure all required columns are present in run list
    try:
        req_cols_file_name = os.path.join(getattr(sys, "_MEIPASS", os.getcwd()),"required_columns.csv")
        with open(req_cols_file_name, "r") as f:
            reader = csv.reader(f)
            required_columns = list(reader)[0]
        for col in required_columns:
            run_list_df = pd.read_csv(run_list)
            assert col in run_list_df, col
    except AssertionError as missing_col:
        return f"{missing_col} column missing, run list may be out of date."
    except FileNotFoundError:
        return "Please run app from project directory."

    # no errors to report
    return ""


def divide_chunks(l, n): 
      
    # looping till length l 
    for i in range(0, len(l), n):  
        yield l[i:i + n]


def get_outage_bounds(row):
    outage1start = row['OUTAGE_1_START'].item()
    outage1end = row['OUTAGE_1_END'].item()
    outage2start = row['OUTAGE_2_START'].item()
    outage2end = row['OUTAGE_2_END'].item()
    outage1type = row['1ST_OUTAGE'].item()

    # compute and return heating and cooling outages
    if outage1type == 'HEATING':
        heatingOutageStart, heatingOutageEnd, coolingOutageStart, coolingOutageEnd = outage1start, outage1end, outage2start, outage2end
    else:
        heatingOutageStart, heatingOutageEnd, coolingOutageStart, coolingOutageEnd = outage2start, outage2end, outage1start, outage1end

    return heatingOutageStart, heatingOutageEnd, coolingOutageStart, coolingOutageEnd


# TODO: HOW TO HANDLE CHECKPOINTS FOR BATCH SIMULATE?
def checkpoint(sm: SimulationManager):
    """Returns true if program received stop signal, otherwise sends progress update and returns false"""
    # gui not enabled, exit
    if sm is None: return

    # check for stop signal
    if sm.check_if_stopped(): raise GracefulExitException
    
    # all clear, update progress
    sm.send_progress()


# TODO: HOOK GUI UP TO INNER FUNCTION AND RETIRE PARALLEL_SIMULATE
def parallel_simulate(sim_inputs: SimInputs, progress_queue=None, stop_event=None):
    
    # move to study folder for all sims
    os.chdir(sim_inputs.study_folder)

    # run the parallelized simulation
    sim_mgr = SimulationManager(progress_queue, sim_inputs.total_runs, stop_event)
    simulate_with_gui_communication(sim_inputs, sim_mgr)
    
#     # export results of any completed runs
#     ResultsTable = pd.concat(result_rows)
#     if sm.check_if_stopped():
#         return
#     ResultsTable.to_csv(os.path.join(sim_inputs.study_folder, sim_inputs.batch_name + "_ResultsTable.csv"))
#     print('Saved Results')


def simulate_with_gui_communication(si: SimInputs, sm: SimulationManager):
    """Check for any graceful exit requests, otherwise communicate error received to gui."""
    # attempt to run the simulation with error handling
    try:
        # perform resilience simulations
        idfs_batch1 = parallel_runner(resilience_simulation_prep, si, sm)
        batch_simulation(si, idfs_batch1, "BR")

        # process intermediate results
        parallel_runner(process_resilience_simulation_output, si, sm)

        # generate the idfs for annual simulation
        idfs_batch2 = parallel_runner(annual_simulation_prep, si, sm)
        batch_simulation(si, idfs_batch2, "BA")

        # collect the results here
        ##################################

        # optional generate graphs/cleanup here
        ##################################
    
    # check for graceful exit request
    except GracefulExitException:
        return
    
    # collect and export here
    print("Saved Results!")
    return


def parallel_runner(fn, si: SimInputs, simulation_mgr=None):
    """Runs the specified simulate function in a multiprocessing pool."""
    # create the pool
    with mp_pool.ThreadPool(processes=si.num_procs) as pool:
        # run the function
        try:
            results = pool.starmap(error_handler, [(fn, si, case_id, simulation_mgr) for case_id in range(si.total_runs)])
        
        # otherwise, pass the error to the gui
        except GracefulExitException:
            pool.terminate()
            return
    
    # return results
    return results


def error_handler(fn, si: SimInputs, case_id: int, simulation_mgr=None):
    """Send any errors back to the gui and return None for run result if caught."""
    # run the function here
    try:
        return fn(si, case_id, simulation_mgr)

    # if caught, pass the graceful exit signal along
    except GracefulExitException:
        raise GracefulExitException
    
    # otherwise pass the error to the gui
    except Exception as e:
        simulation_mgr.raise_exception(str(e))
        return None



def resilience_simulation_prep(si: SimInputs, case_id: int, simulation_mgr=None):
    """Generates and returns IDF for a resilience simulation based on runlist inputs."""
    # CHECKPOINT: resilience prep started
    checkpoint(simulation_mgr)       

    # retrieve cached information from simulation input
    iddfile = si.idd_file
    studyFolder = si.study_folder
    databaseDir = si.database_dir

    weatherDatabase = si.weather_db
    constructionDatabase = si.construction_db

    IDF.setiddname(iddfile)

    runList = si.run_list_df
    runCount = case_id
    idfgName = str(runList['GEOMETRY_IDF'][runCount])
    caseName = runList['CASE_NAME'][runCount]

    # establish file names
    BaseFileName = f"{si.batch_name}_{caseName}"
    testingFile_BR = os.path.join(studyFolder, BaseFileName + "_BR.idf")
    passIDF = os.path.join(studyFolder, BaseFileName + "_PASS.idf")

    epwFile = os.path.join(weatherDatabase, str(runList['EPW'][runCount]))
    ddyName =  os.path.join(weatherDatabase, str(runList['DDY'][runCount]))
    
    # validate spreadsheet input
    assert os.path.isfile(epwFile), "Cannot find specified EPW file."
    assert os.path.isfile(ddyName), "Cannot find specified DDY file."

    icfa = runList['ICFA'][runCount]
    icfa_M  = icfa*0.09290304
    Nbr = runList['BEDROOMS'][runCount]
    occ = int(runList['BEDROOMS'][runCount]) + 1
    operableArea_N = ((runList['Operable_Area_N'][runCount])*0.09290304)
    operableArea_S = ((runList['Operable_Area_S'][runCount])*0.09290304)
    operableArea_W = ((runList['Operable_Area_W'][runCount])*0.09290304)
    operableArea_E = ((runList['Operable_Area_E'][runCount])*0.09290304)
    halfHeight = 1.524
    ervSense = 0
    ervLatent = 0

    # IHG Calc
    constructionList = pd.read_csv(constructionDatabase, index_col="Name")
    appliance_list = list(runList['APPLIANCE_LIST'][runCount].split(', '))

    total_appliance_cost = fridge = dishWasher = clothesWasher = clothesDryer = lights_cost = 0
    ihg_dict = {}
    for Nbr in range(9):
        for appliance_name, row in constructionList.filter(items=appliance_list, axis=0).iterrows():
            rating = float(row["Appliance_Rating"]) # must be float for fractional efficiency
            cost = int(row["Mechanical Cost"])
            if 'FRIDGE' in appliance_name:
                fridge += (rating/(8760))*1000 # always on design load

            elif 'DISHWASHER' in appliance_name:
                dishWasher += (((86.3 + (47.73 / (215 / rating)))/215) * ((88.4 + 34.9*Nbr)*(12/12))*(1/365)*1000)

            elif 'CLOTHESWASHER' in appliance_name:
                clothesWasher += (rating/365)*1000

            elif 'CLOTHESDRYER' in appliance_name:
                clothesDryer += ((12.4*(164+46.5*Nbr)*1.18/3.01*(2.874/0.817-704/rating)/(0.2184*(4.5*4.08+0.24)))/365)*1000

            elif 'LIGHTS' in appliance_name:
                fracHighEff = rating
                lights_cost += cost
        
            total_appliance_cost += cost
        ihg_dict[Nbr] = {'fridge':fridge, 'dishwasher':dishWasher, 'clotheswasher':clothesWasher,
                         'clothesdryer':clothesDryer, 'lighting efficacy':fracHighEff, 
                         'lightsCost':lights_cost, 'applianceCost':total_appliance_cost}
    
    # export IHG dictionary to json for use in annual simulation
    # TODO: ASSIGN TO VARIABLE IN SIMULATION MANAGER
    # TODO: DELETE TEMPORARY FILES LIKE THIS UPON COMPLETION/EXIT
    with open(os.path.join(studyFolder, "ihg_dict.json"), "w") as fp:
        json.dump(ihg_dict, fp)
    
    # TODO: REMOVE LATER
    constructionList = constructionList.reset_index()
    PV_SIZE = runList['PV_SIZE_[W]'][runCount]
    PV_TILT = runList['PV_TILT'][runCount]
    
    # Envelope

    infiltration_rate = runList['INFILTRATION_RATE'][runCount]
    Ext_Window1 = runList['EXT_WINDOW_1'][runCount]
    Ext_Window2 = runList['EXT_WINDOW_2'][runCount]
    Ext_Window3 = runList['EXT_WINDOW_3'][runCount]

    Ext_Wall1 = runList['EXT_WALL_1_NAME'][runCount]
    Ext_Roof1 = runList['EXT_ROOF_1_NAME'][runCount]
    Ext_Floor1 = runList['EXT_FLOOR_1_NAME'][runCount]
    Ext_Door1 = runList['EXT_DOOR_1_NAME'][runCount]
    Int_Floor1 = runList['INT_FLOOR_1_NAME'][runCount]

    Ext_Wall2 = runList['EXT_WALL_2_NAME'][runCount]
    Ext_Roof2 = runList['EXT_ROOF_2_NAME'][runCount]
    Ext_Floor2 = runList['EXT_FLOOR_2_NAME'][runCount]
    Ext_Door2 = runList['EXT_DOOR_2_NAME'][runCount]
    Int_Floor2 = runList['INT_FLOOR_2_NAME'][runCount]

    Ext_Wall3 = runList['EXT_WALL_3_NAME'][runCount]
    Ext_Roof3 = runList['EXT_ROOF_3_NAME'][runCount]
    Ext_Floor3 = runList['EXT_FLOOR_3_NAME'][runCount]
    Ext_Door3 = runList['EXT_DOOR_3_NAME'][runCount]
    Int_Floor3 = runList['INT_FLOOR_3_NAME'][runCount]

    # Foundation interfaces
    fnd1 = runList['FOUNDATION_INTERFACE_1'][runCount]
    fnd1i = runList['FOUNDATION_INSUINSULATION_1'][runCount]
    fnd1p = runList['FOUNDATION_PERIMETER_1'][runCount]
    fnd1d = runList['FOUNDATION_INSULATION_DEPTH_1'][runCount]

    fnd2 = runList['FOUNDATION_INTERFACE_2'][runCount]
    fnd2i = runList['FOUNDATION_INSUINSULATION_2'][runCount]
    fnd2p = runList['FOUNDATION_PERIMETER_2'][runCount]
    fnd2d = runList['FOUNDATION_INSULATION_DEPTH_2'][runCount]

    fnd3 = runList['FOUNDATION_INTERFACE_3'][runCount]
    fnd3i = runList['FOUNDATION_INSUINSULATION_3'][runCount]
    fnd3p = runList['FOUNDATION_PERIMETER_3'][runCount]
    fnd3d = runList['FOUNDATION_INSULATION_DEPTH_3'][runCount]


    if str(fnd1) != 'nan':
        foundationList = [(fnd1,fnd1i,fnd1d,fnd1p)]

    if str(fnd2) != 'nan':
        foundationList = [(fnd1,fnd1i,fnd1d,fnd1p),
                            (fnd2,fnd2i,fnd2d,fnd2p)]
    
    if str(fnd3) != 'nan':
        foundationList = [(fnd1,fnd1i,fnd1d,fnd1p),
                            (fnd2,fnd2i,fnd2d,fnd2p),
                            (fnd3,fnd3i,fnd3d,fnd3p)]

    # Schedule Based Inputs
    _, _, coolingOutageStart, coolingOutageEnd = get_outage_bounds(runList.iloc[[runCount]])
    outage1start = runList['OUTAGE_1_START'][runCount]
    outage1end = runList['OUTAGE_1_END'][runCount]
    outage2start = runList['OUTAGE_2_START'][runCount]
    outage2end = runList['OUTAGE_2_END'][runCount]
    outage1type = runList['1ST_OUTAGE'][runCount]
    
    # Weather Morph inputs
    MorphFactorDB1 = runList['MorphFactorDB1'][runCount]
    MorphFactorDP1 = runList['MorphFactorDP1'][runCount]
    MorphFactorDB2 = runList['MorphFactorDB2'][runCount]
    MorphFactorDP2 = runList['MorphFactorDP2'][runCount]

    # Controls 
    NatVentType  = str(runList['NAT_VENT_TYPE'][runCount])
    NatVentAvail = runList['NAT_VENT_AVAIL'][runCount]
    shadingAvail = runList['SHADING_AVAIL'][runCount]
    demandCoolingAvail = runList['DEMAND_COOLING_AVAIL'][runCount]

    # Mechanical Inputs
    natGasPresent = runList['NATURAL_GAS'][runCount]
    dhwFuel = runList['WATER_HEATER_FUEL'][runCount]
    mechSystemType = runList['MECH_SYSTEM_TYPE'][runCount]

    gridRegion = runList['GRID_REGION'][runCount]
    #==============================================================================================================================
    # 4. Base IDF
    #==============================================================================================================================
    open(testingFile_BR, "w").close()
    idfg = IDF(os.path.join(studyFolder, idfgName))
    ddy = IDF(ddyName)
    idf1 = IDF(testingFile_BR)

    # CHECKPOINT: IDFs constructed
    checkpoint(simulation_mgr)

    # Copy in geometry from input file

    for zone in idfg.idfobjects['Zone']:
        idf1.copyidfobject(zone)

    for srf in idfg.idfobjects['BuildingSurface:Detailed']:
        idf1.copyidfobject(srf)
    
    srf_dict = {}
    for srf in idf1.idfobjects['BuildingSurface:Detailed']:
        zone_name = srf.Zone_Name.split('|')
        srf.Zone_Name = zone_name[0]
        srf_dict[srf.Name] = zone_name[0]
    

    count = -1
    windowNames = []
    windows_by_zone = {}
    for fen in idfg.idfobjects['FenestrationSurface:Detailed']:
        idf1.copyidfobject(fen)
        count += 1
        windows = idf1.idfobjects['FenestrationSurface:Detailed'][count]
        if windows.Surface_Type == 'Window':
            windowNames.append(windows.Name)
            srf_name = windows.Building_Surface_Name
            window_zone = srf_dict[srf_name]
            existing_list = windows_by_zone.get(zone_name[0],[])
            windows_by_zone[window_zone] = existing_list + [windows.Name]

    # site shading

    for site in idfg.idfobjects['Shading:Site:Detailed']:
        idf1.copyidfobject(site)

    for bldg in idfg.idfobjects['Shading:Building:Detailed']:
        idf1.copyidfobject(bldg)

    # sizing data

    for bldg in ddy.idfobjects['SizingPeriod:DesignDay']:
        idf1.copyidfobject(bldg)
    
    # CHECKPOINT: Geometries copied in
    checkpoint(simulation_mgr)

    
    # High level model information
    simControl.Version(idf1)
    simControl.SimulationControl(idf1)
    simControl.Building(idf1,BaseFileName)
    simControl.CO2Balance(idf1)
    simControl.Timestep(idf1)
    simControl.RunPeriod(idf1)
    simControl.GeometryRules(idf1)

    modeled_zones = idf1.idfobjects['ZONE']
    DHW_CombinedGPM = 0

    unit_list = []
    for zone in modeled_zones:
        zone_name = zone.Name.split('|')
        zone_type = zone_name[1] if len(zone_name)>1 else ""
        zone.Name = zone_name[0]
        if 'UNIT' in zone_type:
            unit_list.append(zone_name[0])
            occ = 1 + float(zone_name[2][0])
            icfa_zone = zone.Floor_Area
            Nbr_zone = float(zone_name[2][0])
            fracHighEff = ihg_dict[float(zone_name[2][0])]['lighting efficacy']
            PhiusLights = (0.2 + 0.8*(4 - 3*fracHighEff)/3.7)*(455 + 0.8*icfa_zone*10.76391) * 0.8 * 1000 * (1/365) #power per day W use Phius calc
            PhiusMELs = ((413 + 69*Nbr_zone + 0.91*icfa_zone*10.76391)/365)*1000*0.8 #consumption per day per phius calc
            rangeElec = ((331 + 39*Nbr_zone)/365)*1000
            
            # DHW Calc per BA
            DHW_ClothesWasher = 2.3 + 0.78*Nbr_zone
            DHW_Dishwasher = 2.26 + 0.75*Nbr_zone
            DHW_Shower = 0.83*(14 + 1.17*Nbr_zone)
            DHW_Bath = 0.83*(3.5+1.17*Nbr_zone)
            DHW_Sinks = 0.83*(12.5+4.16*Nbr_zone)
            DHW_CombinedGPM = (DHW_ClothesWasher + DHW_Dishwasher + DHW_Shower + DHW_Bath + DHW_Sinks)*4.381E-8

            # Sizing loads from ASHRAE 1199-RP

            G_0s = 136  #in W
            G_0l = 20  #in W
            G_cfs = 2.2  #in W
            G_cfl = 0.22  #in W
            G_ocs = 22  #in W
            G_ocl = 12  #in W

            sizingLoadSensible = G_0s + G_cfs*icfa_zone + G_ocs*occ
            sizingLoadLatent = G_0l + G_cfl*icfa_zone + G_ocl*occ
            internalHeatGains.People(idf1, zone_name[0], occ)
            internalHeatGains.LightsMELsAppliances(idf1, zone_name[0], PhiusLights, PhiusMELs, fridge, rangeElec, 
                        clothesDryer,clothesWasher,dishWasher)
            internalHeatGains.SizingLoads(idf1, zone_name[0], sizingLoadSensible, sizingLoadLatent)
            internalHeatGains.ThermalMass(idf1, zone_name[0], icfa_zone)

            envelope.infiltration(idf1, zone_name[0], infiltration_rate)

            envelope.WindowVentilation(idf1, zone_name[0], halfHeight, operableArea_N, operableArea_W, 
            operableArea_S, operableArea_E)

            # insert window sorting here

            zone_windows = windows_by_zone[zone_name[0]]
            windowNames_split = list(divide_chunks(zone_windows, 10))
            for i in range(len(windowNames_split)):
                windowNamesChunk = windowNames_split[i]
                envelope.WindowShadingControl(idf1, zone_name[0], windowNamesChunk)

            hvac.SizingSettings(idf1, zone_name[0])
            hvac.HVACControls(idf1, zone_name[0])
            hvac.ZoneMechConnections(idf1, zone_name[0])
            hvac.HVACBuilder(idf1, zone_name[0], mechSystemType)
            hvac.WaterHeater(idf1, zone_name[0], dhwFuel, DHW_CombinedGPM)
            renewables.demand_limiting(idf1, zone_name[0])

        if 'STAIR' in zone_type:
            print(str(zone_name[0]) + ' is some Stairs')
        if 'CORRIDOR' in zone_type:
            print(str(zone_name[0]) + ' is some a Corridor')
    
    # export unit list for use in annual simulation
    with open(os.path.join(studyFolder, "unit_list.json"), "w") as fp:
        json.dump(unit_list, fp)

    # Materials and constructions
    materials = pd.read_csv(os.path.join(databaseDir, 'Material Database.csv'))
    materialList = materials.shape[0]

    for item in range(materialList):
        envelope.materialBuilder(idf1, materials['NAME'][item], materials['ROUGHNESS'][item], 
                        materials['THICKNESS [m]'][item], materials['CONDUCTIVITY [W/mK]'][item],
                        materials['DENSITY [kg/m3]'][item], materials['SPECIFIC HEAT CAPACITY [J/kgK]'][item])
        
    glazingSpecs = pd.read_csv(os.path.join(databaseDir, 'Window Database.csv'))

    glazings = glazingSpecs.shape[0]

    for item in range(glazings):
        envelope.glazingBuilder(idf1, glazingSpecs['NAME'][item], glazingSpecs['U-FACTOR [W/m2K]'][item],glazingSpecs['SHGC'][item])

    # Constructions 

    constructions = constructionList.shape[0]

    for item in range(constructions):
        if str(constructionList['Outside_Layer'][item]) != 'nan':
            layers = [constructionList['Outside_Layer'][item],
            constructionList['Layer_2'][item],
            constructionList['Layer_3'][item],
            constructionList['Layer_4'][item],
            constructionList['Layer_5'][item],
            constructionList['Layer_6'][item],
            constructionList['Layer_7'][item],
            constructionList['Layer_8'][item],
            constructionList['Layer_9'][item],
            constructionList['Layer_10'][item]]

            layerList = [x for x in layers if str(x) != 'nan']

        envelope.constructionBuilder(idf1, constructionList['Name'][item],layerList)

    # Envelope inputs
    
    envelope.SpecialMaterials(idf1)
    envelope.FoundationInterface(idf1,foundationList)
    envelope.ShadeMaterials(idf1)
    envelope.AssignContructions(idf1, Ext_Wall1,Ext_Wall2,Ext_Wall3,
            Ext_Roof1,Ext_Roof2,Ext_Roof3,
            Ext_Floor1,Ext_Floor2,Ext_Floor3,
            Ext_Door1,Ext_Door2,Ext_Door3, 
            Int_Floor1,Int_Floor2,Int_Floor3,
            Ext_Window1,Ext_Window2,Ext_Window3)

    # Sizing settings:
    hvac.Curves(idf1)

    renewables.generators(idf1, PV_SIZE, PV_TILT)

    internalHeatGains.ext_lights(idf1)

    outputs.SimulationOutputs(idf1)
    
    # save the current idf state as pass file
    idf1.saveas(passIDF)
    idf1 = IDF(passIDF)
    
    # CHECKPOINT: before resilience schedules
    checkpoint(simulation_mgr)

    schedules.zeroSch(idf1, 'BARangeSchedule')
    schedules.zeroSch(idf1, 'Phius_Lighting')
    schedules.zeroSch(idf1, 'Phius_MELs')
    schedules.zeroSch(idf1, 'CombinedDHWSchedule')
    schedules.zeroSch(idf1, 'BAClothesDryerSchedule')
    schedules.zeroSch(idf1, 'BAClothesWasherSchedule')
    schedules.zeroSch(idf1, 'BADishwasherSchedule')
    schedules.ResilienceSchedules(idf1, outage1start, outage1end, outage2start, outage2end, 
                                  coolingOutageStart,coolingOutageEnd,NatVentAvail,
                                  demandCoolingAvail,shadingAvail,outage1type)
    
    schedules.ResilienceControls(idf1, unit_list, NatVentType)
    
    # CHECKPOINT: resilience schedules finished
    checkpoint(simulation_mgr)
    
    for zone in unit_list:
        hvac.ResilienceERV(idf1, zone, occ, ervSense, ervLatent)

    weatherMorph.WeatherMorphSine(idf1, outage1start, outage1end, outage2start, outage2end,
            MorphFactorDB1, MorphFactorDP1, MorphFactorDB2, MorphFactorDP2)

    # CHECKPOINT: before resilience simulation starts
    checkpoint(simulation_mgr)

    # save and return the resulting idf
    idf1.saveas(testingFile_BR)
    idf = IDF(testingFile_BR, epwFile)
    return idf


def process_resilience_simulation_output(si: SimInputs, case_id: int, simulation_mgr=None):
    """Collects intermediate information and exports it for easier access in annual simulation."""
    
    # CHECKPOINT: intermediate processing started
    checkpoint(simulation_mgr)

    # infer case run details
    runList = si.run_list_df
    runCount = case_id
    studyFolder = si.study_folder
    BaseFileName = f"{si.batch_name}_{runList['CASE_NAME'][runCount]}"
    
    # load the results
    resil_sim_temp_results = os.path.join(studyFolder, f"{BaseFileName}_BRout.csv")
    hourly = pd.read_csv(resil_sim_temp_results)

    # combine columns
    hourly.rename(columns = {'Date/Time':'DateTime'}, inplace = True)
    hourly[['Date2','Time']] = hourly.DateTime.str.split(expand=True)
    hourly['Date'] = hourly['Date2'].map(str)
    hourly['Time'] = (pd.to_numeric(hourly['Time'].str.split(':').str[0])-1).astype(str).apply(lambda x: f'0{x}' if len(x)==1 else x) + hourly['Time'].str[2:]
    hourly['DateTime'] = hourly['Date'] + ' ' + hourly['Time']
    hourly['DateTime'] = pd.to_datetime(hourly['DateTime'], format="%m/%d %H:%M:%S", exact=True)

    # drop the warmup
    endWarmup = hourly[hourly['DateTime'] == '1900-01-01 00:00:00'].index[0]
    dropWarmup = [*range(0, endWarmup,1)]
    hourly = hourly.drop(index = dropWarmup)
    hourly = hourly.reset_index()

    # get outage start and ends
    heatingOutageStart,heatingOutageEnd,coolingOutageStart,coolingOutageEnd = get_outage_bounds(runList.iloc[[runCount]])
    
    heatingOutageStart1 = dt.strptime((str(heatingOutageStart)), '%d-%b') + timedelta(hours=24)
    coolingOutageStart1 = dt.strptime((str(coolingOutageStart)), '%d-%b') + timedelta(hours=24)
    heatingOutageEnd1 = dt.strptime((str(heatingOutageEnd)), '%d-%b') + timedelta(hours=23)
    coolingOutageEnd1 = dt.strptime((str(coolingOutageEnd)), '%d-%b') + timedelta(hours=23)

    maskh = (hourly['DateTime'] >= heatingOutageStart1) & (hourly['DateTime'] <= heatingOutageEnd1)
    maskc = (hourly['DateTime'] >= coolingOutageStart1) & (hourly['DateTime'] <= coolingOutageEnd1)

    hourlyHeat = hourly.loc[maskh]
    hourlyCool = hourly.loc[maskc]

    # export results
    hourlyHeat.to_csv(os.path.join(studyFolder, f"{BaseFileName}_HourlyHeat.csv"))
    hourlyCool.to_csv(os.path.join(studyFolder, f"{BaseFileName}_HourlyCool.csv"))

    # CHECKPOINT: intermediate processing finished
    checkpoint(simulation_mgr)


def annual_simulation_prep(si: SimInputs, case_id: int, simulation_mgr=None):
    """Generates and returns IDF for an annual simulation based on runlist inputs."""

    # CHECKPOINT: annual prep started
    checkpoint(simulation_mgr) 

    # infer case run details
    runList = si.run_list_df
    runCount = case_id
    studyFolder = si.study_folder
    BaseFileName = f"{si.batch_name}_{runList['CASE_NAME'][runCount]}"
    testingFile_BA = os.path.join(studyFolder, BaseFileName + "_BA.idf")
    epwFile = os.path.join(si.weather_db, runList['EPW'][runCount])

    # check for the output idf, return if not found (something went wrong)
    prev_sim_htm_results = os.path.join(studyFolder, f"{BaseFileName}_BRtbl.htm")
    prev_sim_idf = os.path.join(studyFolder, f"{BaseFileName}_BR.idf")
    prev_sim_temp_results = os.path.join(studyFolder, f"{BaseFileName}_BRout.csv")
    prev_sim_hourly_heat = os.path.join(studyFolder, f"{BaseFileName}_HourlyHeat.csv")
    prev_sim_hourly_cool = os.path.join(studyFolder, f"{BaseFileName}_HourlyCool.csv")
    if (not os.path.isfile(prev_sim_htm_results) or 
        not os.path.isfile(prev_sim_idf) or
        not os.path.isfile(prev_sim_temp_results)): 
        return

    # initialize idfs for simulation
    passIDF = os.path.join(studyFolder, BaseFileName + "_PASS.idf")
    idf1 = IDF(prev_sim_idf)
    idf2 = IDF(passIDF)

    # compute outage starts
    outage1start = runList['OUTAGE_1_START'][runCount]
    outage1end = runList['OUTAGE_1_END'][runCount]
    outage2start = runList['OUTAGE_2_START'][runCount]
    outage2end = runList['OUTAGE_2_END'][runCount]
    _, _, coolingOutageStart, coolingOutageEnd = get_outage_bounds(runList.iloc[[runCount]])
    
    # get other controls info for schedule from runlist
    NatVentAvail = runList['NAT_VENT_AVAIL'][runCount]
    shadingAvail = runList['SHADING_AVAIL'][runCount]
    demandCoolingAvail = runList['DEMAND_COOLING_AVAIL'][runCount]

    # plug into schedules input
    schedules.AnnualSchedules(idf2, outage1start, outage1end, outage2start, outage2end, 
                    coolingOutageStart,coolingOutageEnd,NatVentAvail,
                    demandCoolingAvail,shadingAvail)
    
    # get annual erv info
    occ = runList['BEDROOMS'][runCount] + 1
    ervSense = 0 # TODO: are these correct? values never changed
    ervLatent = 0

    # loop through zones for annual erv computation
    modeled_zones = idf1.idfobjects['ZONE']
    for zone in modeled_zones:
        zone_name = zone.Name
        hvac.AnnualERV(idf2, zone_name, occ, ervSense, ervLatent)
        
    # create schedules for units with windows
    with open(os.path.join(studyFolder, "unit_list.json"), "r") as fp:
        unit_list = json.load(fp)
    schedules.AnnualControls(idf2, unit_list)

    # get envelope information
    infiltration_rate = runList['INFILTRATION_RATE'][runCount]
    mechSystemType = runList['MECH_SYSTEM_TYPE'][runCount]
    dhwFuel = runList['WATER_HEATER_FUEL'][runCount]
    PV_SIZE = runList['PV_SIZE_[W]'][runCount]
    icfa = runList['ICFA'][runCount]
    
    # get hourly temperature results
    hourlyHeat = pd.read_csv(prev_sim_hourly_heat)
    hourlyCool = pd.read_csv(prev_sim_hourly_cool)
    heatingBattery = (hourlyHeat['Whole Building:Facility Total Purchased Electricity Energy [J](Hourly)'].sum())*0.0000002778
    coolingBattery = (hourlyCool['Whole Building:Facility Total Purchased Electricity Energy [J](Hourly)'].sum())*0.0000002778

    # add envelope costs from construction database
    constructionList = pd.read_csv(si.construction_db, index_col="Name")
    for name, row in constructionList.iterrows():
        outerLayer = row['Outside_Layer']
        cost = row['Cost_Per_Area_[$/m2]']
        costSealing = row['Air_Sealing_Cost_[$/ft2 ICFA]']
        costBatt = row['Battery_Cost_[$/kWh]']
        costPV = row['PV_Cost_[$/W]']
        costMech = row['Mechanical Cost']

        if cost > 0 and str(outerLayer) != 'nan':
            envelope.costBuilder(idf2, name, '','Construction', name,'','',cost,'')
        
        if costSealing > 0 and name == infiltration_rate:
            envelope.costBuilder(idf2, f"AIR SEALING = {name}",'','General',0,0,(costSealing*icfa),'',1)

        if costMech> 0 and name == mechSystemType:
            envelope.costBuilder(idf2, f"MECH_{name}",'','General',0,0,costMech,'',1)

        if costMech> 0 and dhwFuel in name:
            envelope.costBuilder(idf2, name,'','General',0,0,costMech,'',1)
        
        if costBatt > 0 and str(outerLayer) == 'nan':
            envelope.costBuilder(idf2, name,'' ,'General',0,0,(costBatt*max(heatingBattery,coolingBattery)),'',1)

        if costPV > 0 and str(outerLayer) == 'nan':
            envelope.costBuilder(idf2, name,'' ,'General',0,0,(costPV*PV_SIZE),'',1)

    # factor in appliances and lights cost to envelope
    with open(os.path.join(studyFolder, "ihg_dict.json"), "r") as fp:
        ihg_dict = json.load(fp)
    
    num_beds = runList['BEDROOMS'][runCount]
    total_appliance_cost = ihg_dict[str(num_beds)]["applianceCost"]
    lights_cost = ihg_dict[str(num_beds)]["lightsCost"]
    envelope.costBuilder(idf2, 'APPLIANCES','','General',0,0,total_appliance_cost,'',1)
    envelope.costBuilder(idf2, 'LIGHTS','','General',0,0,lights_cost,'',1)

    # CHECKPOINT: before annual simulation starts
    checkpoint(simulation_mgr)

    # save and return the resulting idf
    idf2.saveas(testingFile_BA)
    idf = IDF(testingFile_BA, epwFile)
    return idf


def batch_simulation(si: SimInputs, idfs, label: str):
    """Uses eppy batch run to perform resilience simulation"""
    runs = []
    for i, idf in enumerate(idfs):
        if idf is not None:
            # recover case name
            batch_name = si.batch_name
            case_name = si.run_list_df["CASE_NAME"][i]

            # recover energyplus version number
            idfversion = idf.idfobjects['version'][0].Version_Identifier.split('.')
            idfversion.extend([0] * (3 - len(idfversion)))
            idfversionstr = '-'.join([str(item) for item in idfversion])

            # create kwargs dictionary
            options = {"readvars": True,
                       "output_prefix": f"{batch_name}_{case_name}_{label}",
                       "ep_version":idfversionstr}
            
            # prepare the run
            runs.append([idf, options])
    
    # run the simulation
    runIDFs([x for x in runs], processors=si.num_procs)


def collect_simulation_results():
    """Collect all output files from simulation and packs them into one report."""
    pass


def generate_graphs():
    """If designated, create graphs and visualizations of results."""
    pass


def cleanup_outputs():
    """If designated, remove files generated from energyplus (only keep aggregated results)."""
    pass
