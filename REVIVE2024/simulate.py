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
from joblib import Parallel, delayed
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

    def __init__(self, batch_name, idd_file, study_folder, run_list, database_dir, graphs_enabled, pdf_enabled, is_dummy_mode):
        # collect basic input information
        self.batch_name = batch_name
        self.idd_file = idd_file
        self.study_folder = study_folder
        self.run_list = run_list
        self.database_dir = database_dir
        self.graphs_enabled = graphs_enabled
        self.pdf_enabled = pdf_enabled
        self.is_dummy_mode = is_dummy_mode

        # generate the implied input information
        self.generate_database_inputs()

        # generate the results table format
        self.generate_results_table()
    

    def generate_database_inputs(self):
        # emissions and weather data
        emissions_file = "Hourly Emission Rates.csv"
        weather_folder = "Weather Data"
        construction_file = "Construction Database.csv"
        self.emissions_db = os.path.join(self.database_dir, emissions_file)
        self.weather_db = os.path.join(self.database_dir, weather_folder)
        self.construction_db = os.path.join(self.database_dir, construction_file)


    def generate_results_table(self):
        self.results_table = pd.DataFrame(columns=["Run Name","SET ≤ 12.2°C Hours (F)","Hours < 2°C [hr]",'Total Deadly Days','Min outdoor DB [°C]','Min outdoor DP [°C]',
                                                    'Max outdoor DB [°C]','Max outdoor DP [°C]',"Caution (> 26.7, ≤ 32.2°C) [hr]","Extreme Caution (> 32.2, ≤ 39.4°C) [hr]",
                                                    "Danger (> 39.4, ≤ 51.7°C) [hr]","Extreme Danger (> 51.7°C) [hr]", 'EUI','Peak Electric Demand [W]',
                                                    'Heating Battery Size [kWh]', 'Cooling Battery Size [kWh]', 'Total ADORB Cost [$]','First Year Electric Cost [$]',
                                                    'First Year Gas Cost [$]','First Cost [$]','Wall Cost [$]','Roof Cost [$]','Floor Cost [$]','Window Cost [$]',
                                                    'Door Cost [$]','Air Sealing Cost [$]','Mechanical Cost [$]','Water Heater Cost [$]','Appliances Cost [$]','PV Cost [$]',
                                                    'Battery Cost [$]','pv_dirEn_tot','pv_dirMR_tot','pv_opCO2_tot','pv_emCO2_tot','pv_eTrans_tot'])



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


def parallel_simulate(batch_name, idd_file, study_folder, run_list, database_dir, num_procs, graphs_enabled, pdf_enabled, is_dummy_mode):
    # create simulation input object with all non-changing attributes
    sim_inputs = SimInputs(batch_name, idd_file, study_folder, run_list, database_dir, graphs_enabled, pdf_enabled, is_dummy_mode)
    
    # move to study folder for all sims
    if not is_dummy_mode:
        os.chdir(sim_inputs.study_folder)

    # run the parallelized simulation
    runList = pd.read_csv(run_list)
    total_runs = runList.shape[0]
    Parallel(n_jobs=int(num_procs))(delayed(simulate)(sim_inputs, case) for case in range(total_runs))

    ResultsTable = sim_inputs.results_table
    ResultsTable.to_csv(os.path.join(study_folder, batch_name + "_ResultsTable.csv"))
    print('Saved Results')


def simulate(si: SimInputs, case_id: int):
    # retrieve cached information from simulation input
    is_dummy_mode = si.is_dummy_mode
    batchName = si.batch_name if not is_dummy_mode else "dummy"
    iddfile = si.idd_file
    studyFolder = si.study_folder if not is_dummy_mode else os.path.abspath("dummy")
    run_list_path = si.run_list if not is_dummy_mode else os.path.join(studyFolder, "dummy_runlist.csv")
    databaseDir = si.database_dir
    graphs = si.graphs_enabled
    pdfReport = si.pdf_enabled

    emissionsDatabase = si.emissions_db
    weatherDatabase = si.weather_db
    constructionDatabase = si.construction_db

    IDF.setiddname(iddfile)

    runList = pd.read_csv(run_list_path)
    runCount = case_id
    idfgName = str(runList['GEOMETRY_IDF'][runCount])
    BaseFileName = str(batchName + '_' + runList['CASE_NAME'][runCount])
    caseName = runList['CASE_NAME'][runCount]

    print('Running: ' + str(BaseFileName))

    # testingFile = str(studyFolder) + "/" + str(BaseFileName) + ".idf"
    testingFile_BA = os.path.join(studyFolder, BaseFileName + "_BA.idf")
    testingFile_BR = os.path.join(studyFolder, BaseFileName + "_BR.idf")
    passIDF = os.path.join(studyFolder, BaseFileName + "_PASS.idf")

    #==============================================================================================================================
    # 4.0 Variable Assignment
    #==============================================================================================================================

    epwFile = os.path.join(weatherDatabase, str(runList['EPW'][runCount]))
    ddyName =  os.path.join(weatherDatabase, str(runList['DDY'][runCount]))
    
    # validate spreadsheet input
    assert os.path.isfile(epwFile), "Cannot find specified EPW file."
    assert os.path.isfile(ddyName), "Cannot find specified DDY file."

    try:
        # zone_name = 'Zone 1'

        icfa = runList['ICFA'][runCount]
        icfa_M  =icfa*0.09290304
        Nbr = runList['BEDROOMS'][runCount]
        occ = (runList['BEDROOMS'][runCount] + 1)
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
                                'clothesdryer':clothesDryer, 'lighting efficacy':fracHighEff, 'applianceCost':total_appliance_cost}
            
        
        # REMOVE LATER
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

        if outage1type == 'HEATING':
            heatingOutageStart = outage1start
            heatingOutageEnd = outage1end
            coolingOutageStart = outage2start
            coolingOutageEnd = outage2end
        else:
            heatingOutageStart = outage2start
            heatingOutageEnd = outage2end
            coolingOutageStart = outage1start
            coolingOutageEnd = outage1end 

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

        open(str(testingFile_BR), 'w')
        idfg = IDF(os.path.join(studyFolder, idfgName))
        ddy = IDF(ddyName)
        idf1 = IDF(str(testingFile_BR))

        # Copy in geometry from input file

        for zone in idfg.idfobjects['Zone']:
            idf1.copyidfobject(zone)

        for srf in idfg.idfobjects['BuildingSurface:Detailed']:
            idf1.copyidfobject(srf)

        count = -1
        windowNames = []
        for fen in idfg.idfobjects['FenestrationSurface:Detailed']:
            idf1.copyidfobject(fen)
            count += 1
            windows = idf1.idfobjects['FenestrationSurface:Detailed'][count]
            if windows.Surface_Type == 'Window':
                windowNames.append(windows.Name)

        # site shading

        for site in idfg.idfobjects['Shading:Site:Detailed']:
            idf1.copyidfobject(site)

        for bldg in idfg.idfobjects['Shading:Building:Detailed']:
            idf1.copyidfobject(bldg)

        # sizing data

        for bldg in ddy.idfobjects['SizingPeriod:DesignDay']:
            idf1.copyidfobject(bldg)

        
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

        windowNames_split = list(divide_chunks(windowNames, 10))

        for i in range(len(windowNames_split)):
            windowNamesChunk = windowNames_split[i]
            envelope.WindowShadingControl(idf1, windowNamesChunk)

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
        # ============================================================================
        # Pass IDF 
        # ============================================================================
        
        idf1.saveas(str(passIDF))

        # ============================================================================
        # Resilience Specific
        # ============================================================================

        idf1 = IDF(str(passIDF))

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

        for zone in unit_list:
            hvac.ResilienceERV(idf1, zone, occ, ervSense, ervLatent)

        weatherMorph.WeatherMorphSine(idf1, outage1start, outage1end, outage2start, outage2end,
                MorphFactorDB1, MorphFactorDP1, MorphFactorDB2, MorphFactorDP2)


        # ==================================================================================================================================
        # Run Resilience Simulation and Collect Results
        # ==================================================================================================================================

        # add the index in from the for loop for the number of runs to make this table happen faster

        # run the simulation or generate dummy files for speed
        if not is_dummy_mode:
            idf1.saveas(str(testingFile_BR))
            idf = IDF(str(testingFile_BR), str(epwFile))
            idf.run(readvars=True,output_prefix=str(str(BaseFileName) + "_BR"))

        fname = os.path.join(studyFolder, BaseFileName + '_BRtbl.htm')

        site_source_energy_table = fasthtml.tablebyname(open(fname, 'r'), "Site and Source Energy")
        eui = float(site_source_energy_table[1][1][2])

        time_bin_table = fasthtml.tablebyname(open(fname, 'r'), "Time Bin Results")
        Below2C = float(time_bin_table[1][39][2])

        heating_set_hours_table = fasthtml.tablebyname(open(fname, 'r'), "Heating SET Hours")
        HeatingSET = float(heating_set_hours_table[1][1][1])

        heating_index_hours_table = fasthtml.tablebyname(open(fname, 'r'), "Heat Index Hours")
        Caution = float(heating_index_hours_table[1][1][2])
        ExtremeCaution = float(heating_index_hours_table[1][1][3])
        Danger = float(heating_index_hours_table[1][1][4])
        ExtremeDanger = float(heating_index_hours_table[1][1][5])

        # Resilience Graphs
                
        filehandle = os.path.join(studyFolder, BaseFileName + '_BRout.csv')
        hourly = pd.read_csv(filehandle)
        hourlyBA = pd.read_csv(filehandle)

        hourly.rename(columns = {'Date/Time':'DateTime'}, inplace = True)
        hourly[['Date2','Time']] = hourly.DateTime.str.split(expand=True)
        hourly['Date'] = hourly['Date2'].map(str) + '/' + str(2020)
        hourly['Time'] = (pd.to_numeric(hourly['Time'].str.split(':').str[0])-1).astype(str).apply(lambda x: f'0{x}' if len(x)==1 else x) + hourly['Time'].str[2:]
        hourly['DateTime'] = hourly['Date'] + ' ' + hourly['Time']
        hourly['DateTime'] = pd.to_datetime(hourly['DateTime'], format="%m/%d/%Y %H:%M:%S", exact=True)

        endWarmup = hourly[hourly['DateTime'] == '2020-01-01 00:00:00'].index[0]
        dropWarmup = [*range(0, endWarmup,1)]

        hourly = hourly.drop(index = dropWarmup)
        hourly = hourly.reset_index()

        heatingOutageStart1 = dt.strptime((str(heatingOutageStart) + '/' + str(2020)), '%m/%d/%Y') + timedelta(hours=24)
        coolingOutageStart1 = dt.strptime((str(coolingOutageStart) + '/' + str(2020)), '%m/%d/%Y') + timedelta(hours=24)
        heatingOutageEnd1 = dt.strptime((str(heatingOutageEnd) + '/' + str(2020)), '%m/%d/%Y') + timedelta(hours=23)
        coolingOutageEnd1 = dt.strptime((str(coolingOutageEnd) + '/' + str(2020)), '%m/%d/%Y') + timedelta(hours=23)

        maskh = (hourly['DateTime'] >= heatingOutageStart1) & (hourly['DateTime'] <= heatingOutageEnd1)
        maskc = (hourly['DateTime'] >= coolingOutageStart1) & (hourly['DateTime'] <= coolingOutageEnd1)

        hourlyHeat = hourly.loc[maskh]
        hourlyCool = hourly.loc[maskc]

        MinDBOut = min(hourlyHeat['Environment:Site Outdoor Air Drybulb Temperature [C](Hourly)'].tolist())
        MinDPOut = min(hourlyHeat['Environment:Site Outdoor Air Dewpoint Temperature [C](Hourly)'].tolist())
        MaxDBOut = max(hourlyCool['Environment:Site Outdoor Air Drybulb Temperature [C](Hourly)'].tolist())
        MaxDPOut = max(hourlyCool['Environment:Site Outdoor Air Dewpoint Temperature [C](Hourly)'].tolist())

        x = hourlyHeat['DateTime']
        if graphs == True:
            fig = plt.figure(layout='constrained', figsize=(10, 10))
            fig.suptitle((str(caseName) + '_Heating Outage Resilience'), fontsize='x-large')
            ax = fig.subplot_mosaic([['temperature'],['rh'],['SET']])
            ax['temperature'].plot(x,hourlyHeat["Environment:Site Outdoor Air Drybulb Temperature [C](Hourly)"], label="Site Dry Bulb [C]", linestyle='dashed')
            ax['temperature'].plot(x,hourlyHeat["ZONE 1:Zone Air Temperature [C](Hourly)"], label="Zone Dry Bulb [C]",color='black',linewidth=2)
            ax['temperature'].set_ylim(((min(min(hourlyHeat["Environment:Site Outdoor Air Drybulb Temperature [C](Hourly)"]), min(hourlyHeat["ZONE 1:Zone Air Temperature [C](Hourly)"])))-5),((max(max(hourlyHeat["Environment:Site Outdoor Air Drybulb Temperature [C](Hourly)"]), max(hourlyHeat["ZONE 1:Zone Air Temperature [C](Hourly)"])))+5))
            ax['temperature'].set_ylabel('Temperature [C]')
            ax['temperature'].legend(ncol=2, loc='lower left', borderaxespad=0, fontsize='x-small')
            ax['temperature'].grid(True)

            ax['rh'].plot(x,hourlyHeat['ZONE 1:Zone Air Relative Humidity [%](Hourly)'], label=("Zone RH"),color='black',linewidth=2)
            ax['rh'].set_ylabel('Relative Humidity [%]')
            ax['rh'].set_ylim(0,100)
            ax['rh'].legend(ncol=2, loc='lower left', borderaxespad=0, fontsize='x-small')
            ax['rh'].grid(True)

            ax['SET'].plot(x,hourlyHeat['ZONE OCCUPANTS:Zone Thermal Comfort Pierce Model Standard Effective Temperature [C](Hourly)'], label=("Zone SET"),color='black',linewidth=2)
            ax['SET'].grid(True)
            ax['SET'].legend(ncol=2, loc='lower left', borderaxespad=0, fontsize='x-small')
            ax['SET'].set_ylim((min(hourlyHeat['ZONE OCCUPANTS:Zone Thermal Comfort Pierce Model Standard Effective Temperature [C](Hourly)'])-5),(max(hourlyHeat['ZONE OCCUPANTS:Zone Thermal Comfort Pierce Model Standard Effective Temperature [C](Hourly)'])+5))
            ax['SET'].set_xlabel('Date')
            ax['SET'].set_ylabel('Standard Effective Temperature [°C]')
            ax['SET'].axhline(12.2, color='crimson', linestyle='dashed')

            heatingGraphFile = os.path.join(studyFolder, BaseFileName + "_Heating Outage Resilience Graphs.png")

            plt.savefig(str(heatingGraphFile), dpi=300)
            plt.clf()

        x = hourlyCool['DateTime']
        if graphs == True:
            fig = plt.figure(layout='constrained', figsize=(10, 10))
            fig.suptitle((str(caseName) + '_Cooling Outage Resilience'), fontsize='x-large')
            ax = fig.subplot_mosaic([['temperature'],['rh'],['HI']])
            ax['temperature'].plot(x,hourlyCool["Environment:Site Outdoor Air Drybulb Temperature [C](Hourly)"], label="Site Dry Bulb [C]", linestyle='dashed')
            ax['temperature'].plot(x,hourlyCool["ZONE 1:Zone Air Temperature [C](Hourly)"], label="Zone Dry Bulb [C]",color='black',linewidth=2)
            ax['temperature'].set_ylim(((min(min(hourlyCool["Environment:Site Outdoor Air Drybulb Temperature [C](Hourly)"]), min(hourlyCool["ZONE 1:Zone Air Temperature [C](Hourly)"])))-5),((max(max(hourlyCool["Environment:Site Outdoor Air Drybulb Temperature [C](Hourly)"]), max(hourlyCool["ZONE 1:Zone Air Temperature [C](Hourly)"])))+5))
            ax['temperature'].set_ylabel('Temperature [C]')
            ax['temperature'].legend(ncol=2, loc='lower left', borderaxespad=0, fontsize='x-small')
            ax['temperature'].grid(True)

            ax['rh'].plot(x,hourlyCool['ZONE 1:Zone Air Relative Humidity [%](Hourly)'], label=("Zone RH"),color='black',linewidth=2)
            ax['rh'].set_ylabel('Relative Humidity [%]')
            ax['rh'].set_ylim(0,100)
            ax['rh'].legend(ncol=2, loc='lower left', borderaxespad=0, fontsize='x-small')
            ax['rh'].grid(True)

            ax['HI'].plot(x,hourlyCool['ZONE 1:Zone Heat Index [C](Hourly)'], label=("Zone HI"),color='black',linewidth=2)
            ax['HI'].grid(True)
            ax['HI'].legend(ncol=2, loc='lower left', borderaxespad=0, fontsize='x-small')
            ax['HI'].set_ylim((min(hourlyCool['ZONE 1:Zone Heat Index [C](Hourly)'])-5),(max(hourlyCool['ZONE 1:Zone Heat Index [C](Hourly)'])+5))
            ax['HI'].set_xlabel('Date')
            ax['HI'].set_ylabel('Heat Index [°C]')
            ax['HI'].axhline(26.7, color='seagreen', linestyle='dashed')
            ax['HI'].axhline(32.2, color='orange', linestyle='dashed')
            ax['HI'].axhline(39.4, color='crimson', linestyle='dashed')
            ax['HI'].axhline(51.7, color='darkmagenta', linestyle='dashed')

            coolingGraphFile = os.path.join(studyFolder, BaseFileName + "_Cooling Outage Resilience Graphs.png")

            plt.savefig(str(coolingGraphFile), dpi=300)
            plt.clf()
            del fig
        gc.collect()

        # Mora days

        RH = hourlyCool['ZONE 1:Zone Air Relative Humidity [%](Hourly)'].tolist()
        Temp = hourlyCool['ZONE 1:Zone Air Temperature [C](Hourly)'].tolist()

        RHdays = [RH[x:x+24] for x in range(0, len(RH), 24)]
        TEMPdays = [Temp[x:x+24] for x in range(0, len(Temp), 24)]

        avgRH = []
        avgTemp = []
        moraDays = []
        moraPF = []
        moraTotalDays = 0
        for day in range(7):
            avgRH.append(mean(RHdays[day]))
            avgTemp.append(mean(TEMPdays[day]))
            moraDays.append((49.593 - 48.580*np.array(mean(RHdays[day])*0.01) +25.887*np.array(mean(RHdays[day])*0.01)**2))
            moraPF.append(mean(TEMPdays[day])-((49.593 - 48.580*np.array(mean(RHdays[day])*0.01) +25.887*np.array(mean(RHdays[day])*0.01)**2)))
            if (mean(TEMPdays[day])-((49.593 - 48.580*np.array(mean(RHdays[day])*0.01) +25.887*np.array(mean(RHdays[day])*0.01)**2))) > 0:
                moraTotalDays = moraTotalDays+1

        # Battery Sizing
        heatingBattery = (hourlyHeat['Whole Building:Facility Total Purchased Electricity Energy [J](Hourly)'].sum())*0.0000002778
        coolingBattery = (hourlyCool['Whole Building:Facility Total Purchased Electricity Energy [J](Hourly)'].sum())*0.0000002778

        # hourlyHeat.to_csv(str(studyFolder) + "/" + str(BaseFileName) + "_hourlyHeat.csv")
        # hourlyCool.to_csv(str(studyFolder) + "/" + str(BaseFileName) + "_hourlyCool.csv")

        # Save HTML and CSV outputs
        reportHTML = os.path.join(studyFolder, 'eplustbl.htm')
        reportCSV = os.path.join(studyFolder, 'eplusout.csv')
        reportSQL= os.path.join(studyFolder, 'eplusout.sql')
        reportHTML2 = os.path.join(studyFolder, BaseFileName + '_BR_eplustbl.htm')
        reportCSV2 = os.path.join(studyFolder, BaseFileName + '_BR_eplusout.csv')
        reportSQL2= os.path.join(studyFolder, BaseFileName + '_BR_eplusout.sql')


        # if os.path.exists(reportCSV2):
        #     os.remove(reportCSV2)
        
        # if os.path.exists(reportHTML2):
        #     os.remove(reportHTML2)

        # if os.path.exists(reportSQL2):
        #     os.remove(reportSQL2)

        # os.rename(reportHTML,reportHTML2)
        # os.rename(reportCSV,reportCSV2)
        # os.rename(reportSQL,reportSQL2)

        # ============================================================================
        # Annual Specific
        # ============================================================================

        idf2 = IDF(str(passIDF))

        schedules.AnnualSchedules(idf2, outage1start, outage1end, outage2start, outage2end, 
                    coolingOutageStart,coolingOutageEnd,NatVentAvail,
                    demandCoolingAvail,shadingAvail)
        for zone in modeled_zones:
            zone_name = zone.Name.split('|')
            zone_type = zone_name[1] if len(zone_name)>1 else ""
            if 'UNIT' in zone_type:
                hvac.AnnualERV(idf2, zone_name[0], occ, ervSense, ervLatent)

        for item in range(constructions):
            
            name = constructionList['Name'][item]
            outerLayer = constructionList['Outside_Layer'][item]
            cost = constructionList['Cost_Per_Area_[$/m2]'][item]
            costSealing = constructionList['Air_Sealing_Cost_[$/ft2 ICFA]'][item]
            costBatt = constructionList['Battery_Cost_[$/kWh]'][item]
            costPV = constructionList['PV_Cost_[$/W]'][item]
            costMech = constructionList['Mechanical Cost'][item]

            if cost > 0 and str(outerLayer) != 'nan':
                envelope.costBuilder(idf2, name, '','Construction', name,'','',cost,'')
            
            if costSealing > 0 and str(name) == str(flowCoefficient):
                envelope.costBuilder(idf2,('AIR SEALING = ' + str(name)),'','General',0,0,(costSealing*icfa),'',1)

            if costMech> 0 and str(name) == str(mechSystemType):
                envelope.costBuilder(idf2, ('MECH_' + str(name)),'','General',0,0,costMech,'',1)

            if costMech> 0 and str(dhwFuel) in str(name):
                envelope.costBuilder(idf2, (str(name)),'','General',0,0,costMech,'',1)
            
            if costBatt > 0 and str(outerLayer) == 'nan':
                envelope.costBuilder(idf2, name,'' ,'General',0,0,(costBatt*max(heatingBattery,coolingBattery)),'',1)

            if costPV > 0 and str(outerLayer) == 'nan':
                envelope.costBuilder(idf2, name,'' ,'General',0,0,(costPV*PV_SIZE),'',1)


        envelope.costBuilder(idf2, ('APPLIANCES'),'','General',0,0,total_appliance_cost,'',1)
        envelope.costBuilder(idf2, ('LIGHTS'),'','General',0,0,lights_cost,'',1)


            
        # Annual Result Collection

        if not is_dummy_mode:
            idf2.saveas(str(testingFile_BA))
            idf = IDF(str(testingFile_BA), str(epwFile))
            idf.run(readvars=True,output_prefix=str((str(BaseFileName) + '_BA')))


        filehandle = os.path.join(studyFolder, BaseFileName + '_BAout.csv')
        filehandleMTR = os.path.join(studyFolder, BaseFileName + '_BAmtr.csv')
        hourly = pd.read_csv(filehandle)
        monthlyMTR= pd.read_csv(filehandleMTR)

        hourly.rename(columns = {'Date/Time':'DateTime'}, inplace = True)
        hourly[['Date2','Time']] = hourly.DateTime.str.split(expand=True)
        hourly['Date'] = hourly['Date2'].map(str) + '/' + str(2020)
        hourly['Time'] = (pd.to_numeric(hourly['Time'].str.split(':').str[0])-1).astype(str).apply(lambda x: f'0{x}' if len(x)==1 else x) + hourly['Time'].str[2:]
        hourly['DateTime'] = hourly['Date'] + ' ' + hourly['Time']
        hourly['DateTime'] = pd.to_datetime(hourly['DateTime'], format="%m/%d/%Y %H:%M:%S", exact=True)

        endWarmup = hourly[hourly['DateTime'] == '2020-01-01 00:00:00'].index[0]
        dropWarmup = [*range(0, endWarmup,1)]

        hourly = hourly.drop(index = dropWarmup)
        hourly = hourly.reset_index()

        fname = os.path.join(studyFolder, BaseFileName + '_BAtbl.htm')
        
        site_source_energy_table = fasthtml.tablebyname(open(fname, 'r'), "Site and Source Energy")
        eui = float(site_source_energy_table[1][1][2])

        annual_peak_values_table = fasthtml.tablebyname(open(fname, 'r'), "Annual and Peak Values - Electricity")
        peakElec = float(annual_peak_values_table[1][1][4])

        if 'BASE' in str(BaseFileName):
            firstCost = [0,0]
            wallCost = 0
            roofCost = 0
            floorCost = 0
            windowCost = 0
            doorCost = 0
            airSealing = 0
            mechCost = 0
            dhwCost = 0
            applianceCost = 0
            lightsCost = 0
            pvCost = 0
            batteryCost = 0
        else:
            wallCostList = []
            roofCostList = []
            floorCostList = []
            windowCostList = []
            doorCostList = []
            airSealingCostList = []
            mechCostList = []
            dhwCostList = []
            applianceCostList = []
            lightsCost = []

            construction_cost_est_table = fasthtml.tablebyname(open(fname, 'r'), "Construction Cost Estimate Summary")
            firstCost = [float(construction_cost_est_table[1][9][2]),0]

            cost_line_item_detail_table = fasthtml.tablebyname(open(fname, 'r'), "Cost Line Item Details")
            rows = len(cost_line_item_detail_table[1])
            for row in range(rows):
                item_name = cost_line_item_detail_table[1][row][2]
                item_cost = cost_line_item_detail_table[1][row][6]
                if 'WALL' in item_name:
                    wallCostList.append(item_cost)
                elif 'ROOF' in item_name:
                    roofCostList.append(item_cost)
                elif 'FLOOR' in item_name:
                    floorCostList.append(item_cost)
                elif 'WINDOW' in item_name:
                    windowCostList.append(item_cost)
                elif 'DOOR' in item_name:
                    doorCostList.append(item_cost)
                elif 'AIR SEALING' in item_name:
                    airSealingCostList.append(item_cost)
                elif 'MECH' in item_name:
                    mechCostList.append(item_cost)
                elif 'DHW' in item_name:
                    dhwCostList.append(item_cost)
                elif 'APPLIANCES' in item_name:
                    applianceCostList.append(item_cost)
                elif 'LIGHTS' in item_name:
                    applianceCostList.append(item_cost)
                elif 'PV COST' in item_name:
                    pvCost = (item_cost)
                elif 'BATTERY COST' in item_name:
                    batteryCost = (item_cost)

            wallCost = (sum(wallCostList))
            roofCost = (sum(roofCostList))
            floorCost = (sum(floorCostList))
            windowCost = (sum(windowCostList))
            doorCost = (sum(doorCostList))
            airSealing = (sum(airSealingCostList))
            mechCost = (sum(mechCostList))
            dhwCost = (sum(dhwCostList))
            applianceCost = (sum(applianceCostList))
            pvCost = pvCost
            batteryCost = batteryCost

        # Save HTML and CSV outputs
        reportHTML = os.path.join(studyFolder, 'eplustbl.htm')
        reportCSV = os.path.join(studyFolder, 'eplusout.csv')
        reportSQL= os.path.join(studyFolder, 'eplusout.sql')
        reportHTML2 = os.path.join(studyFolder, BaseFileName + '_BA_eplustbl.htm')
        reportCSV2 = os.path.join(studyFolder, BaseFileName + '_BA_eplusout.csv')
        reportSQL2= os.path.join(studyFolder, BaseFileName + '_BA_eplusout.sql')

        # if os.path.exists(reportCSV2):
        #     os.remove(reportCSV2)
        
        # if os.path.exists(reportHTML2):
        #     os.remove(reportHTML2)

        # if os.path.exists(reportSQL2):
        #     os.remove(reportSQL2)

        # os.rename(reportHTML,reportHTML2)
        # os.rename(reportCSV,reportCSV2)
        # os.rename(reportSQL,reportSQL2)

    ##################ADORB 436253 RED QUEEN PIN

        hourlyBA.rename(columns = {'Date/Time':'DateTime'}, inplace = True)
        hourlyBA[['Date2','Time']] = hourlyBA.DateTime.str.split(expand=True)
        hourlyBA['Date'] = hourlyBA['Date2'].map(str) + '/' + str(2020)
        hourlyBA['Time'] = (pd.to_numeric(hourlyBA['Time'].str.split(':').str[0])-1).astype(str).apply(lambda x: f'0{x}' if len(x)==1 else x) + hourlyBA['Time'].str[2:]
        hourlyBA['DateTime'] = hourlyBA['Date'] + ' ' + hourlyBA['Time']
        hourlyBA['DateTime'] = pd.to_datetime(hourlyBA['DateTime'], format="%m/%d/%Y %H:%M:%S", exact=True)

        endWarmup = hourlyBA[hourlyBA['DateTime'] == '2020-01-01 00:00:00'].index[0]
        dropWarmup = [*range(0, endWarmup,1)]

        hourlyBA = hourlyBA.drop(index = dropWarmup)
        hourlyBA = hourlyBA.reset_index()

        MWH = hourlyBA['Whole Building:Facility Total Purchased Electricity Energy [J](Hourly)']*0.0000000002778

        CO2_Elec_List = []
        count = 0
        for filename in os.listdir(os.path.join(databaseDir, 'CambiumFactors')):
            if filename.endswith('.csv'):
                hourlyBAEmissions = pd.read_csv(os.path.join(databaseDir, 'CambiumFactors', filename))
                emissions = hourlyBAEmissions[str(gridRegion)]
                CO2_Elec = sum(MWH*emissions)
                count = count + 1
                CO2_Elec_List.append((CO2_Elec))

        annualCO2Elec = CO2_Elec_List


        # CO2_Elec = sum(MWH*emissions)

        gasPrice = runList['GAS_PRICE_[$/THERM]'][runCount]

        if natGasPresent == 1:
            monthlyMTR = monthlyMTR.drop(index=[0,1,2,3,4,5,6,7])
            annualGas = (((sum(monthlyMTR['NaturalGas:Facility [J](Monthly) ']*9.478169879E-9))*gasPrice)+(40*12))
            annualCO2Gas = (sum(monthlyMTR['NaturalGas:Facility [J](Monthly) ']*9.478169879E-9))*12.7
        else:
            CO2_gas = 0
            annualCO2Gas = 0
            annualGas = 0


        # Future above to be better integrated

        duration = int(runList['ANALYSIS_DURATION'][runCount])
        elecPrice = float(runList['ELEC_PRICE_[$/kWh]'][runCount])
        elec_sellback_price = float(runList['SELLBACK_PRICE_[$/kWh]'][runCount])
        annualElec = ((hourly['Whole Building:Facility Total Purchased Electricity Energy [J](Hourly)'].sum()*0.0000002778*elecPrice)-
                        (hourly['Whole Building:Facility Total Surplus Electricity Energy [J](Hourly)'].sum()*0.0000002778*elec_sellback_price)
                        +100)
        
        

        # annualCO2 = CO2_Elec + CO2_gas

        carbonDatabase = pd.read_csv(os.path.join(databaseDir, 'Carbon Correction Database.csv'))
        countryEmissionsDatabase = pd.read_csv(os.path.join(databaseDir, 'Country Emission Database.csv'))

        if str(runList['CARBON_MEASURES'][runCount]) != 'nan':
            carbonMeasures = carbondMeasures = list(runList['CARBON_MEASURES'][runCount].split(', '))

        carbonMeasureCost = []

        for measure in range(carbonDatabase.shape[0]):
            if carbonDatabase['Name'][measure] in carbonMeasures:
                carbonMeasureCost.append([carbonDatabase['Cost'][measure], carbonDatabase['Year'][measure]])

        carbonMeasureCost.append(firstCost)

        emCO2 = []

        for measure in range(carbonDatabase.shape[0]):
            if carbonDatabase['Name'][measure] in carbonMeasures:
                for country in range(countryEmissionsDatabase.shape[0]):
                    if str(countryEmissionsDatabase['COUNTRY'][country]) == str(carbonDatabase['Country'][1]):
                        ef = countryEmissionsDatabase['EF [kg/$]'][country]
                    if str(countryEmissionsDatabase['COUNTRY'][country]) == 'USA':
                        efUSA = countryEmissionsDatabase['EF [kg/$]'][country]
                emCO2.append([(carbonDatabase['Cost'][measure]*ef*(1-carbonDatabase['Labor Fraction'][measure])) + 
                                ((carbonDatabase['Cost'][measure]*efUSA*(carbonDatabase['Labor Fraction'][measure]))), carbonDatabase['Year'][measure]])
                # Labor fraction should be subtracted out and have USA EF applied

        for country in range(countryEmissionsDatabase.shape[0]):
                    if str(countryEmissionsDatabase['COUNTRY'][country]) == str(runList['ENVELOPE_COUNTRY'][runCount]):
                        efENV = countryEmissionsDatabase['EF [kg/$]'][country]
                        emCO2first = ((firstCost[0] * (1-runList['ENVELOPE_LABOR_FRACTION'][runCount]) * efENV) + 
                            (firstCost[0] * (runList['ENVELOPE_LABOR_FRACTION'][runCount]) * efUSA))

                        emCO2firstCost = [emCO2first,0]

        emCO2.append(emCO2firstCost)

        # print(carbonMeasureCost)

        dirMR = carbonMeasureCost


        # EMBODIED CARBON CALCULATION
        
        # get emissions data ready
        constructionList['Name'] = constructionList['Name'].apply(lambda x: x.lower())
        constructionList = constructionList.set_index("Name")
        countryEmissionsDatabase = countryEmissionsDatabase.set_index("COUNTRY")
        country = runList['ENVELOPE_COUNTRY'][runCount]
        price_of_carbon = 0.25 # units: $/kg according to spec
        # TODO: AVOID HARDCODING CARBON MULTIPLE TIMES

        # define routine to compute the embodied CO2 and direct maintenance costs for ADORB
        def add_item_to_adorb_inputs(name, cost=None):
            emissions_factor = countryEmissionsDatabase.loc[country, 'EF [kg/$]']
            try:
                # cross-reference with construction list if item exists
                labor_fraction = constructionList.loc[name, "Labor_Fraction"]
                lifetime = int(constructionList.loc[name, "Lifetime"])
                if cost==None: cost = constructionList.loc[name, "Mechanical Cost"]
            except KeyError:
                print(f"Could not find \"{name}\" in construction database.")
                return
            embodied_carbon_calc = (cost * (1 - labor_fraction)) * (emissions_factor * price_of_carbon)
            
            # add cost anytime item needs installed or replaced
            if lifetime != 0:
                for year in range(0, duration, lifetime):
                    dirMR.append([cost, year])
                    emCO2.append([embodied_carbon_calc, year])
            else:
                dirMR.append([cost, 0])
                emCO2.append([embodied_carbon_calc, 0])
        

        # extract cost line item subtotals
        fname = os.path.join(studyFolder, BaseFileName + '_BAtbl.htm')
        cost_line_item_detail_table = fasthtml.tablebyname(open(fname, 'r'), "Cost Line Item Details")
        cost_line_df = pd.DataFrame(cost_line_item_detail_table[1][1:],
                                    columns=cost_line_item_detail_table[1][0]).iloc[:-1] # drop the last summation row

        # compute emCO2 and dirMR per non-zero line item
        cost_line_df_subgroup = cost_line_df[cost_line_df["Quantity."] > 0]
        for _, row in cost_line_df_subgroup.iterrows():

            # extract basic information
            item_name = row["Item Name"].lower()
            item_cost = row["SubTotal $"]

            # strip any mechanical labels if neccessary
            if item_name[:5] == "mech_": item_name = item_name[5:]

            # handle appliance breakdown after loop
            if item_name == "appliances" or item_name == "lights":
                continue
            
            # for all normal entries compute and add to emCO2 and dirMR list
            else:
                add_item_to_adorb_inputs(item_name, item_cost)
        
        # compute emCO2 and dirMR per each appliance/lights 
        for appliance_name in appliance_list:
            add_item_to_adorb_inputs(appliance_name.lower())

        # emCO2 = [(emCO2_firstCost,1),((8500*laborFraction*0.3),20),((8500*laborFraction*0.3),40),((8500*laborFraction*0.3),60)] 
        eTrans = peakElec
        
        final = adorb.adorb(BaseFileName, studyFolder, duration, annualElec, annualGas, annualCO2Elec, annualCO2Gas, dirMR, emCO2, eTrans, graphs)

        adorbCost = final[0]
        pv_dirEn_tot = final[1]
        pv_dirMR_tot = final[2]
        pv_opCO2_tot = final[3]
        pv_emCO2_tot = final[4]
        pv_eTrans_tot = final[5]

        # ===============================================================================================================
        # Final Result Collection
        # ===============================================================================================================
        newResultRow = pd.DataFrame([{'Run Name':runList['CASE_NAME'][runCount],
                                            'SET ≤ 12.2°C Hours (F)':HeatingSET,
                                            "Hours < 2°C [hr]":Below2C,
                                            'Total Deadly Days':moraTotalDays,
                                            'Min outdoor DB [°C]':MinDBOut,
                                            'Min outdoor DP [°C]':MinDPOut,
                                            'Max outdoor DB [°C]':MaxDBOut,
                                            'Max outdoor DP [°C]':MaxDPOut,
                                            "Caution (> 26.7, ≤ 32.2°C) [hr]":Caution,
                                            "Extreme Caution (> 32.2, ≤ 39.4°C) [hr]":ExtremeCaution,
                                            "Danger (> 39.4, ≤ 51.7°C) [hr]":Danger,
                                            "Extreme Danger (> 51.7°C) [hr]":ExtremeDanger,
                                            'EUI':eui,
                                            'Peak Electric Demand [W]':peakElec,
                                            'Heating Battery Size [kWh]':heatingBattery, 
                                            'Cooling Battery Size [kWh]':coolingBattery,
                                            'First Year Electric Cost [$]' : annualElec,
                                            'First Year Gas Cost [$]':annualGas,
                                            'First Cost [$]':firstCost[0],
                                            'Wall Cost [$]':wallCost,
                                            'Roof Cost [$]':roofCost,
                                            'Floor Cost [$]':floorCost,
                                            'Window Cost [$]':windowCost,
                                            'Door Cost [$]':doorCost,
                                            'Air Sealing Cost [$]':airSealing,
                                            'Mechanical Cost [$]':mechCost,
                                            'Water Heater Cost [$]':dhwCost,
                                            'Appliances Cost [$]':applianceCost,
                                            'PV Cost [$]':pvCost,
                                            'Battery Cost [$]':batteryCost,
                                            'Total ADORB Cost [$]':adorbCost,
                                            'pv_dirEn_tot':pv_dirEn_tot,
                                            'pv_dirMR_tot':pv_dirMR_tot,
                                            'pv_opCO2_tot':pv_opCO2_tot,
                                            'pv_emCO2_tot':pv_emCO2_tot,
                                            'pv_eTrans_tot':pv_eTrans_tot}])

        newResultRow.to_csv(os.path.join(studyFolder, caseName + "_Test_ResultsTable.csv"))
        
        si.results_table = pd.concat([si.results_table, newResultRow], axis=0, ignore_index=True)#, ignore_index=True)

        if pdfReport == True:

            outputs.PDF_Report(caseName, studyFolder, HeatingSET, Below2C, Caution, ExtremeCaution, Danger, ExtremeDanger, 
                    heatingBattery, coolingBattery, eui, peakElec, annualElec, annualGas,
                    firstCost, adorbCost, heatingGraphFile, coolingGraphFile, adorb.adorbWedgeGraph,
                    adorb.adorbBarGraph)

    except Exception as e:
        # errorFile1= (str(studyFolder) + '\eplusout.err')
        # errorFile2 = (str(studyFolder) + "/" + str(BaseFileName)  + '_BA_eplusout.sql')
        
        # if os.path.exists(errorFile2):
        #     os.remove(errorFile2)
        #     os.rename(errorFile1,errorFile2)
        newResultRow = pd.DataFrame([{'Run Name':runList['CASE_NAME'][runCount],
                                                'SET ≤ 12.2°C Hours (F)':'ERROR',
                                                "Hours < 2°C [hr]":'ERROR',
                                                'Total Deadly Days':'ERROR',
                                                'Min outdoor DB [°C]':'ERROR',
                                                'Min outdoor DP [°C]':'ERROR',
                                                'Max outdoor DB [°C]':'ERROR',
                                                'Max outdoor DP [°C]':'ERROR',
                                                "Caution (> 26.7, ≤ 32.2°C) [hr]":'ERROR',
                                                "Extreme Caution (> 32.2, ≤ 39.4°C) [hr]":'ERROR',
                                                "Danger (> 39.4, ≤ 51.7°C) [hr]":'ERROR',
                                                "Extreme Danger (> 51.7°C) [hr]":'ERROR',
                                                'EUI':'ERROR',
                                                'Peak Electric Demand [W]':'ERROR',
                                                'Heating Battery Size [kWh]':'ERROR', 
                                                'Cooling Battery Size [kWh]':'ERROR',
                                                'First Year Electric Cost [$]':'ERROR',
                                                'First Year Gas Cost [$]':'ERROR',
                                                'First Cost [$]':'ERROR',
                                                'Wall Cost [$]':'ERROR',
                                                'Roof Cost [$]':'ERROR',
                                                'Floor Cost [$]':'ERROR',
                                                'Window Cost [$]':'ERROR',
                                                'Door Cost [$]':'ERROR',
                                                'Air Sealing Cost [$]':'ERROR',
                                                'Mechanical Cost [$]':'ERROR',
                                                'Water Heater Cost [$]':'ERROR',
                                                'Appliances Cost [$]':'ERROR',
                                                'PV Cost [$]':'ERROR',
                                                'Battery Cost [$]':'ERROR',
                                                'Total ADORB Cost [$]':'ERROR',
                                                'pv_dirEn_tot':'ERROR',
                                                'pv_dirMR_tot':'ERROR',
                                                'pv_opCO2_tot':'ERROR',
                                                'pv_emCO2_tot':'ERROR',
                                                'pv_eTrans_tot':'ERROR'}])

        newResultRow.to_csv(os.path.join(studyFolder, caseName + "_Test_ResultsTable.csv"))
        
        si.results_table = pd.concat([si.results_table, newResultRow], axis=0, ignore_index=True)#, ignore_index=Truue
        raise Exception(e)
        # print('Saved Results')