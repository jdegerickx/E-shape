import openeo
from openeo.rest.conversions import timeseries_json_to_pandas
import pandas as pd
from openeo.rest.job import RESTJob
import geopandas as gpd
import shapely
from shapely.ops import transform
from functools import partial
import pyproj
import geojson
import numpy as np
import os
import scipy.signal
import json
import requests
import time
import logging
import zipfile
import glob
import re
import uuid
from shapely.geometry import  Polygon
import matplotlib.pyplot as plt
from tensorflow.keras.models import load_model
from datetime import timedelta


def Openeo_extraction_S1_VH_VV(field_test,start,end,S1_Passes,connection):
    dict_metrics = {}
    for S1_pass in S1_Passes:
        datacube = connection.load_collection('S1_GRD_SIGMA0_{}'.format(S1_pass))
        s1_time_series = datacube.filter_temporal(start, end).polygonal_mean_timeseries(field_test.geometry) \
                     .execute()
        df_S1 = timeseries_json_to_pandas(s1_time_series)
        columns_original = list(df_S1.columns.values)
        columns_new = ['VH', 'VV', 'angle']
        df_S1.rename(columns=dict(zip(columns_original,columns_new)), inplace=True)
        dict_metrics.update({'S1_{}_{}'.format(S1_pass, field_test.unique_ID): df_S1})
    return dict_metrics
def OpenEO_extraction_cropSAR(start,end,outdir, shp_dir, connection):
    dict_metrics = {}
    request = {
        "process_graph": {
            "cropsar": {
                "process_id": "cropsar",
                "arguments": {
                    "polygon_file": shp_dir,
                    "start_date": start,
                    "end_date" : end
                },
                "result": True
            }
        }
    }
    job = connection.create_job(request["process_graph"], additional= { "driver-memory": "25g","driver-cores": "6","driver-memoryOverhead": "6g","executor-memory": "2g","executor-memoryOverhead": "1500m","executor-cores": "2"})

    return job
def submit_job_openeo_CropSAR(job, outdir):
    RESTJob.run_synchronous(job, outdir)
    with zipfile.ZipFile(outdir,'r') as zip_ref:
        zip_ref.extractall('.')
    return zip_ref

    # cropsar_time_series = connection.datacube_from_process("cropsar",polygon_file=r"S:\eshape\Pilot 1\data\WIG_data\2018_WIG_planting_harvest_dates_subset.shp",start_date=start,
    #                                                        end_date=end).execute_batch(r'S:\eshape\Pilot 1\data\WIG_data\cropsar_test.shp', job_options={
    #     "driver-memory": "10g",
    #     "driver-cores": "6",
    #     "driver-memoryOverhead": "6g",
    #     "executor-memory": "2g",
    #     "executor-memoryOverhead": "1500m",
    #     "executor-cores": "2",
    # })
    # # print('test')


def data_requests(url, geometry=None, params=None):
    """
    Request the data from an URL. Perform 3 retries after failing
    :param method: HTTP method to use
    :param url:  URL to which the request needs to be send
    :param geometry:  The geometry to include
    :param params: The URL parameters
    :return: Tuple containing the response and status code
    """
    limit = 3
    amount_loops_requests = 0
    request_status = 0
    data_request = None
    while request_status != 200 and amount_loops_requests < limit:
        if amount_loops_requests > 0:
            time.sleep(10)
        try:
            logging.debug('Requesting ({}/{})'.format(amount_loops_requests + 1, limit))

            data_request = requests.post(url=url, params=params, json=geometry)

            request_status = data_request.status_code
            amount_loops_requests += 1
        except:
            request_status = 0
            amount_loops_requests += 1
    return data_request, request_status


def get_geometry(field, region='Flanders'):
    ###################### DEFINE GEOMETRY OF POLGYON #######################
    #########################################################################
    geometry = field['geometry']
    geometry = shapely.ops.transform(lambda x, y, z=0: (x, y), geometry)
    #TODO: reproject to crs 4326 for all polys
    field['geometry'] = geometry
    geometry = geojson.Feature(geometry=geometry, properties={"ID": 'PEURTENERS'}).geometry
    bbox = field.geometry
    minx, miny, maxx, maxy = bbox.bounds
    bbox = bbox.bounds
    return bbox, geometry, minx, miny, maxx, maxy, field
def TSS_service_CropSAR_extraction(field_test,start,end,crs):
    ################# REQUEST CROPSAR TIMESERIES FOR POLYGON###############
    dict_metrics = {}
    typerequest = "POST"
    bbox, geometry, minx, miny, maxx, maxy, field = get_geometry(field_test)
    URL = 'https://cropsar.vito.be/api/v1.0/cropsar-analysis/'
    params = {'product': 'S2_FAPAR', 'start': start, 'end': end, 'crs': crs['init'], 'source': 'probav-mep'}
    post_r, post_r_status = data_requests(URL, geometry, params)
    cropsar_dict = post_r._content.decode('utf8').replace("'", '"')
    cropsar_dict = json.loads(cropsar_dict)
    timestamps = [item for item in cropsar_dict['cropsar']]
    greenness = [cropsar_dict['cropsar'][timestamps[p]]['q50'] for p in range(len(timestamps))]
    dict_metrics.update({'fAPAR_{}'.format(field_test.unique_ID):pd.DataFrame(greenness,index= pd.to_datetime(timestamps), columns=['fAPAR'])})
    return dict_metrics

def concat_df_from_Openeo(dict_directory, data_source = 'no_double_dates'):
    year_data = int(next(iter(dict_directory)).rsplit('_')[2])
    df_concat = pd.concat((pd.read_csv(dict_directory.get(key)) for key in dict_directory), axis= 1)
    df_concat = df_concat.rename(columns={'Unnamed: 0': 'Date'})
    if data_source == 'WIG_2019':
        date_column = df_concat.Date.iloc[:,0]
        df_concat.drop(columns=['Date'], inplace= True) ## keep only one date column if there are multiple (cropsar case)
        df_concat['Date'] = date_column
    df_concat.Date = pd.to_datetime(df_concat.Date)
    mask_year = ((df_concat.Date >= pd.to_datetime('{}-01-01'.format(year_data))) & (df_concat.Date <= pd.to_datetime('{}-12-31'.format(year_data)))) #retain only the values of the yera of interest
    df_concat = df_concat.loc[mask_year]
    df_concat.index = df_concat.Date
    df_concat = df_concat.drop(columns=['Date'])
    return df_concat


def fAPAR_CropSAR_concat_OpenEO(dict_dir_shp_id, df_cropsar_dict):
    dict_df_ids = {}
    for key in dict_dir_shp_id:
        shp = gpd.read_file(dict_dir_shp_id.get(key))
        ids_fields = shp.id.to_list()
        df_cropsar = df_cropsar_dict.get(key)
        df_cropsar.columns = ids_fields
        dict_df_ids.update({key: df_cropsar})
    return dict_df_ids

def fAPAR_CropSAR_concat_API(dict_dir_shp_id, CropSAR_dir, country_dict): # function to concat all CropSAR extracts of the fields together
    dict_df_cropsar_merged = dict() # the dictionary containing the merged df's of the individual fields
    fields_available = []
    files = glob.glob(os.path.join(CropSAR_dir, '**', '**','parcel_*_cropsar.csv'))
    for f in range(len(files)):
        fields_available.append(files[f].rsplit('parcel_')[1].rsplit('_cropsar.csv')[0])
    fields_available.extend([item.split('fields_')[1] for item in fields_available if 'TAP' in item])
    fields_available = [item for item in fields_available if not 'TAP' in item]
    for key in dict_dir_shp_id:
        dict_df_cropsar_per_field = dict()  # the dictionary containing the df's for each individual field
        year_data = int(key.rsplit('_')[0]) # the year of interest for this specific dataset
        idx = pd.date_range('{}-01-01'.format(int(year_data)), '{}-12-31'.format(int(year_data)))
        shp = gpd.read_file(dict_dir_shp_id.get(key))
        ids_fields = shp.id.to_list()
        ids_shp_available = [item for item in fields_available if item in str(ids_fields)] # the ids for which cropsar data is available in this shapefile
        ids_shp_available = list(set(ids_shp_available))
        if not 'TAP' in key:
            files_shp = [os.path.join(CropSAR_dir, country_dict.get(key),'CropSAR','parcel_{}_cropsar.csv'.format(item)) for item in ids_shp_available] # the directories to cropsar data for the fields in the shp-file
        else:
            files_shp = [os.path.join(CropSAR_dir, country_dict.get(key),'CropSAR','parcel_TAP_Monitoring_fields_{}_cropsar.csv'.format(item)) for item in ids_shp_available] # the directories to cropsar data for the fields in the shp-file

        files_shp = [item for item in files_shp if os.path.exists(item)]
        dict_df_cropsar_per_field.update({'{}'.format(ids_shp_available[f]): pd.read_csv(files_shp[f])['q50'] for f in range(len(files_shp))})
        df_merged = pd.DataFrame.from_dict(dict_df_cropsar_per_field)
        df_merged.index = idx
        dict_df_cropsar_merged.update({'{}'.format(key):df_merged})
    return dict_df_cropsar_merged



def S1_VHVV_ratio_concat(dir_data, datasets_dict, ro_s_dataset): # function to concat all VHVV extracts of the fields together
    dict_df_VHVV = dict()
    for dataset in datasets_dict.keys():
        for ro_interest in ro_s_dataset.get(dataset):
            files_S1 = glob.glob(os.path.join(dir_data, '**', 'S1_*{}.csv').format(ro_interest))
            files_S1 = [item for item in files_S1 if any(S1_pass in item.lower() for S1_pass in ['ascending', 'descending'])]
            file_S1 = [item for item in files_S1 if dataset in item][0]
            shp = gpd.read_file(datasets_dict.get(dataset))
            field_ids = shp.id.to_list()
            df_S1 = pd.read_csv(file_S1)
            df_S1.Date = df_S1.Date.values.astype('datetime64[D]')
            df_S1.index = df_S1.Date
            df_S1 = df_S1.drop(columns=['Date'])
            df_S1 = df_S1.loc[:, df_S1.columns.isin([str('VV_' + str(item)) for item in field_ids] + [str('VH_' + str(item)) for item in field_ids])]  # only the fields of interest for data extraction)]
            for id in field_ids:
                if 'VH_{}'.format(str(id)) in df_S1.columns.values: # in case no data for a field in the dataset for a specific orbit, just neglect it
                    df_S1['VH_VV_{}'.format(id)] = 10 * np.log10(df_S1['VH_{}'.format(id)] / df_S1['VV_{}'.format(id)])
            df_S1 = df_S1.loc[:,df_S1.columns.isin(['VH_VV_' + str(item) for item in field_ids])]
            df_S1.columns = [item.replace('VH_VV_','') for item in df_S1.columns.to_list()] # only keep the unique id of the field in the column
            dict_df_VHVV.update({dataset + '_{}'.format(ro_interest): df_S1})
    return dict_df_VHVV
def coherence_concat(dir_data, datasets_dict, ro_s): # function to concat all coherence extracts of the fields together
    dict_df_coherence = dict()
    for ro_interest in ro_s:
        files_coherence = glob.glob(os.path.join(dir_data, 'S1_coherence*{}.csv').format(ro_interest))
        for dataset in datasets_dict.keys():
            shp = gpd.read_file(datasets_dict.get(dataset))
            if 'Flax' in dataset:
                dataset = 'Flax_fields_2018'
            file_coherence = [item for item in files_coherence if dataset in item][0]
            field_ids = shp.id.to_list()
            df_coherence = pd.read_csv(file_coherence)
            coh_vv_ids = np.arange(0,len(field_ids)*2,2) # only interested in VV coherence since it is more sensitive, the array shows the position of the VV_coh
            df_coherence = df_coherence.rename(columns = {'polygon':'Date'})
            df_coherence = df_coherence.iloc[2:]
            df_coherence.index = pd.to_datetime(df_coherence['Date'])
            df_coherence = df_coherence.drop(columns = ['Date'])
            df_coherence = df_coherence.iloc[:,coh_vv_ids] *0.004 # rescaling factor
            df_coherence.columns = field_ids
            if 'Flax' in dataset:
                dataset = '2018_Flax'
            dict_df_coherence.update({dataset + '_{}'.format(ro_interest): df_coherence})
    return dict_df_coherence



def Plot_time_series_metrics_crop_calendar(datasets_dict, ro_s, metrics, dict_crop_calendars_data, crop_calendar_events, Basefolder, dict_df_cropSAR = False, dict_df_VHVV = False, dict_df_coherence = False, harvest_prob = False):
    # function to plot the time series of the metrics together with the known crop calendar events
    crop_calendar_dates = dict()
    for dataset in datasets_dict:
        year = int(re.search(r'\d+', dataset).group())
        for p in range(len(crop_calendar_events)):
            crop_calendar_dates.update({crop_calendar_events[p]:pd.read_excel(dict_crop_calendars_data.get(dataset))[['id','croptype',crop_calendar_events[p]]]})
        for ro_select in ro_s:
            # define the period for which data will be plotted
            start = '{}-01-01'.format(str(year))
            end = '{}-12-31'.format(str(year))
            ##### loading of df's
            if dict_df_cropSAR:
                df_fAPAR = dict_df_cropSAR.get(dataset)
                ids = list(df_fAPAR.columns)
            if dict_df_coherence:
                df_coherence = dict_df_coherence.get(dataset+'_{}'.format(ro_select))
                ids = list(df_coherence.columns)
            if dict_df_VHVV:
                df_VHVV = dict_df_VHVV.get(dataset + ('_{}'.format(ro_select)))
                ids  = list(df_VHVV.columns)
            for id in ids:
                if dict_df_cropSAR:
                    df_fAPAR_id = df_fAPAR.iloc[:, df_fAPAR.columns.isin([id])]
                if dict_df_VHVV:
                    df_VHVV_id = df_VHVV.iloc[:,df_VHVV.columns.isin([id])]
                    if np.isnan(df_VHVV_id['{}'.format(id)]).all():
                        continue
                if dict_df_coherence:
                    df_coherence_id = df_coherence.iloc[:,df_coherence.columns.isin([id])]
                    if np.isnan(df_coherence_id['{}'.format(id)]).all():
                        continue

                ### generating plot
                ax_names = ['ax_{}'.format(n) for n in range(len(metrics))]
                fig, (ax_names) = plt.subplots(len(metrics), figsize = (15,10))
                if dict_df_cropSAR:
                   df_fAPAR_id.columns = ['CropSAR']
                   df_fAPAR_id.plot(grid=True, ax=ax_names[0], color='green')
                    ### add line for the crop calendars reference data + define croptype from shapefile
                   ax_names[0].set_ylabel('fAPAR')
                   ax_names[0].set_xlabel('Date')
                   #ax1.set_title('fAPAR and coherence for {}'.format(str(crop_type)))
                if dict_df_VHVV:
                    df_VHVV_id.columns = ['VH_VV_ratio_{}'.format(ro_select)]
                    df_VHVV_id.plot(grid = True, ax = ax_names[1], color = 'black')
                    ### add line for the crop calendars reference data + define croptype from shapefile
                    ax_names[1].set_ylabel('VH/VV ratio (dB)')
                    ax_names[1].set_xlabel('Date')
                if dict_df_coherence:
                    df_coherence_id = df_coherence_id.tz_localize(None) # remove timezone (UTC) info
                    df_coherence_id = df_coherence_id.reindex(df_VHVV_id.index) # to allow plotting on the same axis
                    df_coherence_id.columns = ['VV_coherence_{}'.format(ro_select)]
                    df_coherence_id.plot(grid = True, ax = ax_names[2], color = 'blue')
                    ### add line for the crop calendars reference data + define croptype from shapefile
                    ax_names[2].set_ylabel('Coherence')
                    ax_names[2].set_xlabel('Date')
                    missing_crop_event = False
                for s in range(len(crop_calendar_events)):
                    if 'Flax' in dataset:
                        date_event = pd.to_datetime(crop_calendar_dates.get(crop_calendar_events[s]).loc[crop_calendar_dates.get(crop_calendar_events[s]).id == id][crop_calendar_events[s]], dayfirst= True).values[0]
                    else:
                        date_event = pd.to_datetime(crop_calendar_dates.get(crop_calendar_events[s]).loc[crop_calendar_dates.get(crop_calendar_events[s]).id == id][crop_calendar_events[s]]).values[0]
                    if np.isnan(date_event):
                        missing_crop_event = True # if one of the crop events are unknown


                    linestyles = [r'solid', r'dashed', r'dotted']
                    croptype = crop_calendar_dates.get(crop_calendar_events[s]).loc[crop_calendar_dates.get(crop_calendar_events[s]).id == id]['croptype'].values[0]
                    if not missing_crop_event:#only plot if actual crop calendar data available
                        for o in range(len(metrics)):
                            ax_names[o].axvline(x=date_event, color='red', linestyle = linestyles[s], label='{}'.format(crop_calendar_events[s]))
                            ax_names[o].legend(loc = 'upper right')
                ax_names[0].set_title('Crop calendar metrics for {}'.format(croptype))
                plt.tight_layout()
                if not missing_crop_event:
                    if not os.path.exists(os.path.join(Basefolder,'{}'.format(ro_select),'{}'.format(str(year)),'{}'.format(croptype))): os.makedirs(os.path.join(Basefolder,'{}'.format(ro_select),'{}'.format(str(year)),'{}'.format(croptype)))
                    fig.savefig(os.path.join(Basefolder,'{}'.format(ro_select),'{}'.format(str(year)),'{}'.format(croptype), '{}_{}_{}_{}.png'.format(str(year),str(id),'_'.join(metrics),ro_select)))
                else:
                    if not os.path.exists(os.path.join(Basefolder,'{}'.format(ro_select),'{}'.format(str(year)),'{}'.format(croptype), 'No_{}'.format(crop_calendar_events[0]))): os.makedirs(os.path.join(Basefolder,'{}'.format(ro_select),'{}'.format(str(year)),'{}'.format(croptype), 'No_{}'.format(crop_calendar_events[0])))
                    fig.savefig(os.path.join(Basefolder,'{}'.format(ro_select),'{}'.format(str(year)),'{}'.format(croptype, 'No_{}'.format(crop_calendar_events[0])), '{}_{}_{}_{}.png'.format(str(year),str(id),'_'.join(metrics),ro_select)))

                plt.close()

def apply_NN_model(datasets_dict, dir_model,p): # in this function the trained NN model wil be applied on the dataset containing window extracts of the metrics
    iterations = 30
    amount_metrics_model = 5*2
    for key in datasets_dict:
        print('ITERATION: {}'.format(str(p)))
        x_test = datasets_dict.get(key).iloc[0:datasets_dict.get(key).shape[0], 0:amount_metrics_model]
        loaded_model = load_model(os.path.join(dir_model,'model_update1.0_iteration{}.h5'.format(str(p))))
        predictions = loaded_model.predict(x_test)
        ids_orbit = datasets_dict.get(key).index.to_list()
        ids_orbit = [item.rsplit('_',1)[0] for item in ids_orbit]
        datasets_dict.get(key)['predictions_prob'] = predictions
        datasets_dict.get(key)['ID_field_orbit'] = ids_orbit
    return datasets_dict

def dict_dataframes_to_dataframe_field(dict, key_identifier, field_id): ## function that convert the dataframes with the metric for all fields to a dataframe of one field of this metric
    df_metric = dict.get(key_identifier)
    df_metric_id = df_metric.iloc[:,df_metric.columns.isin([field_id])]
    return df_metric_id

def Plot_time_series_metrics_crop_calendar_probability(dict_ro_s, metrics, dict_crop_calendars_dates, crop_calendar_events, Basefolder, country_dataset_dict, dict_crop_calendar_prob = False, dict_df_cropSAR = False, dict_df_VHVV = False, dict_df_coherence = False):
    # function to plot the crop_calendar probabilities out of the NN model togeter with the time series of the metrics used to make the prediction
    crop_calendar_dates = dict()
    for dataset in dict_ro_s:
        ro_s = dict_ro_s.get(dataset)
        year = int(re.search(r'\d+', dataset).group())
        # for p in range(len(crop_calendar_events)):
        #     try:
        #         crop_calendar_dates.update({crop_calendar_events[p]:pd.read_excel(dict_crop_calendars_data.get(dataset))[['id','croptype',crop_calendar_events[p]]]})
        #     except:
        #         crop_calendar_dates.update({crop_calendar_events[p]:pd.read_csv(dict_crop_calendars_data.get(dataset))[['id','croptype',crop_calendar_events[p]]]})

        # define the period for which data will be plotted
        start = '{}-01-01'.format(str(year))
        end = '{}-12-31'.format(str(year))
        ##### loading of df's
        if dict_df_cropSAR:
            df_fAPAR = dict_df_cropSAR.get(dataset)
            ids = list(df_fAPAR.columns)
        for id in ids:
            #check if image not already exists
            if not os.path.exists(os.path.join(os.path.split(Basefolder)[0],country_dataset_dict.get(dataset),dataset, os.path.split(Basefolder)[1])): os.makedirs(os.path.join(os.path.split(Basefolder)[0],country_dataset_dict.get(dataset),dataset, os.path.split(Basefolder)[1]))
            if os.path.exists(os.path.join(os.path.split(Basefolder)[0],country_dataset_dict.get(dataset),dataset,os.path.split(Basefolder)[1],'Harvest_prob_time_series_{}_{}.png'.format('_'.join(metrics),id))):
                continue

            skip_id = False
            if dict_df_cropSAR:
                df_fAPAR_id = dict_dataframes_to_dataframe_field(dict_df_cropSAR, dataset, id)
            if dict_df_VHVV:
                dict_df_VHVV_id = dict()
                for ro in ro_s:
                    dict_df_VHVV_id.update({'VHVV_{}'.format(ro): dict_dataframes_to_dataframe_field(dict_df_VHVV,dataset + '_{}'.format(ro), id)})
                dict_df_VHVV_id = {k: v for (k, v) in dict_df_VHVV_id.items() if not v.empty} # remove the empty dataframes in case no RO orbit is available for this field
                ro_s_available_id = [item.split('_')[1] for item in list(dict_df_VHVV_id.keys())] # the ro's that can be plotted for this field
            if dict_df_coherence:#TODO update likewise as for VHVV in case want to use the coherence data
                dict_df_coherence_id = dict()
                for ro in ro_s:
                    dict_df_coherence_id.update({'coherence_{}'.format(ro): dict_dataframes_to_dataframe_field(dict_df_VHVV,dataset + '_{}'.format(ro), id)})


            ### generating plot
            ax_names = ['ax_{}'.format(n) for n in range(len(ro_s_available_id)+1+1)] # +1 because of the harvest probability plotting #TODO need to be changed in case coherence will be taken into account
            fig, (ax_names) = plt.subplots(len(ro_s_available_id)+1+1, figsize = (15,10)) #TODO need to be changed in case coherence will be taken into account
            if dict_df_cropSAR:
               df_fAPAR_id.columns = ['CropSAR']
               df_fAPAR_id.plot(grid=True, ax=ax_names[0], color='green')
                ### add line for the crop calendars reference data + define croptype from shapefile
               ax_names[0].set_ylabel('fAPAR')
               ax_names[0].set_xlim([df_fAPAR_id.index[0], df_fAPAR_id.index[-1]])
               #ax1.set_title('fAPAR and coherence for {}'.format(str(crop_type)))
            if dict_df_VHVV:
                for s in range(len(ro_s_available_id)):
                    df_VHVV_id = dict_df_VHVV_id.get('VHVV_{}'.format(ro_s_available_id[s]))
                    df_VHVV_id.columns = ['VH_VV_ratio_{}'.format(ro_s_available_id[s])]
                    df_VHVV_id.plot(grid = True, ax = ax_names[1+s], color = 'black')
                    ### add line for the crop calendars reference data + define croptype from shapefile
                    ax_names[1+s].set_ylabel('VH/VV ratio (dB)')
                    ax_names[1+s].set_xlim([df_fAPAR_id.index[0], df_fAPAR_id.index[-1]])
            if dict_df_coherence: #TODO update likewise as for VHVV in case want to use the coherence data
                for s in range(len(ro_s)):
                    df_coherence_id = dict_df_coherence_id.get('coherence_{}'.format(ro_s[s]))
                    df_coherence_id = df_coherence_id.tz_localize(None) # remove timezone (UTC) info
                    df_coherence_id = df_coherence_id.reindex(df_VHVV_id.index) # to allow plotting on the same axis
                    df_coherence_id.columns = ['VV_coherence_{}'.format(ro_s[s])]
                    df_coherence_id.plot(grid = True, ax = ax_names[3+s], color = 'blue')
                    ### add line for the crop calendars reference data + define croptype from shapefile
                    ax_names[3+s].set_ylabel('Coherence')
                    ax_names[3+s].set_xlim([df_fAPAR_id.index[0], df_fAPAR_id.index[-1]])
            if dict_crop_calendar_prob:
                colors = ['red', 'blue', 'green','purple']
                for ro in ro_s_available_id:
                    df_crop_calendar_prob_id = dict_crop_calendar_prob.get('moving_window_extracts_'+dataset+'_{}'.format(crop_calendar_events[0].lower()))
                    df_crop_calendar_prob_id = df_crop_calendar_prob_id.loc[df_crop_calendar_prob_id.ID_field_orbit == id + '_{}'.format(ro)]
                    if df_crop_calendar_prob_id.empty:
                        skip_id = True
                    else:
                        df_crop_calendar_prob_id.index = pd.to_datetime(df_crop_calendar_prob_id.prediction_date_window)
                        df_crop_calendar_prob_id = df_crop_calendar_prob_id[~df_crop_calendar_prob_id.index.duplicated(keep='first')] # remove duplicated in case fields (overlap) with the same ID
                        df_crop_calendar_prob_id = df_crop_calendar_prob_id.reindex(df_fAPAR_id.index) ### to allow a nicer fit
                        df_crop_calendar_prob_id.reset_index().plot.scatter(x = 'index', y = 'predictions_prob', grid = True, ax = ax_names[len(ro_s_available_id)+1], color = colors[ro_s_available_id.index(ro)], label=ro) #TODO need to be changed in case coherence will be taken into account
                        ax_names[len(ro_s_available_id)+1].set_xlim([df_fAPAR_id.index[0], df_fAPAR_id.index[-1]]) #TODO need to be changed in case coherence will be taken into account
                        ax_names[len(ro_s_available_id)+1].set_ylabel('Probability') #TODO need to be changed in case coherence will be taken into account
                        ax_names[len(ro_s_available_id)+1].set_xlabel('Date') #TODO need to be changed in case coherence will be taken into account



                if skip_id:
                    plt.close()
                    continue

                missing_crop_event = False
            for s in range(len(crop_calendar_events)):
                df_ID_cropcalendar_dates = dict_crop_calendars_dates.get(dataset+'_{}'.format(crop_calendar_events[0].lower()))
                if 'Flax' in dataset:
                    date_event = pd.to_datetime(df_ID_cropcalendar_dates.loc[df_ID_cropcalendar_dates.id == id][crop_calendar_events[s].lower()], dayfirst= True).values[0]
                else:
                    date_event =  pd.to_datetime(df_ID_cropcalendar_dates.loc[df_ID_cropcalendar_dates.id == id][crop_calendar_events[s].lower()]).values[0]
                if np.isnan(date_event):
                    missing_crop_event = True # if one of the crop events are unknown


                linestyles = [r'solid', r'dashed', r'dotted']
                try:
                    croptype = crop_calendar_dates.get(crop_calendar_events[s]).loc[crop_calendar_dates.get(crop_calendar_events[s]).id == id]['croptype'].values[0]
                except:
                    croptype = None
                if not missing_crop_event:#only plot if actual crop calendar data available
                    for o in range(len(ax_names)):
                        ax_names[o].axvline(x=date_event, color='red', linestyle = linestyles[s], label='{}'.format(crop_calendar_events[s].split('_')[0]))
                        ax_names[o].legend(loc = 'upper right')
            if croptype is not None:
                ax_names[0].set_title('Crop calendar metrics + probability for {}'.format(croptype))
            else:
                ax_names[0].set_title('Crop calendar metrics + probability')
            plt.tight_layout()

            fig.savefig(os.path.join(os.path.split(Basefolder)[0],country_dataset_dict.get(dataset),dataset,os.path.split(Basefolder)[1],'Harvest_prob_time_series_{}_{}.png'.format('_'.join(metrics),id)))

            plt.close()


def combine_cropcalendar_data(datasets_dict, cropcalendar_data_dict, crop_calendar_events):
    #function to combine the knwon crop calendar events of the fields in a dataset in a nice df
    crop_calendar_dates = dict()
    for dataset in datasets_dict:
        try:
            df_crop_calendar_info =  pd.read_excel(cropcalendar_data_dict.get(dataset))[['id', 'croptype',crop_calendar_events[0].lower()]]
        except:
            df_crop_calendar_info = pd.read_excel(cropcalendar_data_dict.get(dataset))[['id', crop_calendar_events[0].lower()]]
        df_crop_calendar_info = df_crop_calendar_info.astype({'id': str})
        crop_calendar_dates.update({dataset+ '_'+crop_calendar_events[0].lower(): df_crop_calendar_info})
    return crop_calendar_dates


def moving_window_metrics_extraction(datasets_dict, ROs_dataset_dict, dict_VHVV, dict_cropSAR, metrics, VH_VV_range,fAPAR_range, dict_cropcalendars_data, crop_calendar_events):
    # function to extract the metrics data in a certain window to allow the running of the trained NN model
    dict_moving_window_extracts = dict()
    for dataset in datasets_dict:
        df_harvest_model_ro_combined = []
        for ro_interest in ROs_dataset_dict.get(dataset):
            if 'VHVV' in metrics:
                df_VHVV = dict_VHVV.get(dataset+'_'+ ro_interest)
                df_VHVV = 2 * (df_VHVV - VH_VV_range[0]) / (VH_VV_range[1] - VH_VV_range[0]) - 1  # rescale
            if 'CropSAR' in metrics:
                df_cropSAR = dict_cropSAR.get(dataset)
                df_cropSAR = 2 * (df_cropSAR - fAPAR_range[0]) /(fAPAR_range[1] - fAPAR_range[0]) - 1
                df_cropSAR = df_cropSAR.reindex(df_VHVV.index)

            window_values = 5  # 4 coverage are extracted within the window
            moving_window_steps = np.arange(0, df_cropSAR.shape[0] - window_values - 1)
            window_width = (window_values - 1) * 6  # days within the window
            fields_dataset_compiling = gpd.read_file(datasets_dict.get(dataset)).id.to_list()
            df_harvest_model = []
            print('{} FIELDS TO COMPILE IN DATASET ({}) FOR {}'.format(df_VHVV.shape[1], dataset, ro_interest))
            for id in fields_dataset_compiling:
                print('FIELD {}: COMPILING OF {}'.format(fields_dataset_compiling.index(id), id))
                df_cropsar_field = df_cropSAR.loc[:, df_cropSAR.columns.isin([str(id)])]
                df_VHVV_field = df_VHVV.loc[:, df_VHVV.columns.isin([str(id)])]
                if df_VHVV_field.empty or df_cropsar_field.empty: # if no data for that field in the RO go to the next one
                    continue
                df_field_input_data_harvest_model = []
                for p in range(len(moving_window_steps)):
                    ### sample some fAPAR data within the window and derive the difference from the harves date for the selected window
                    df_cropsar_field_window = pd.DataFrame(df_cropsar_field.iloc[p:p + window_values, 0])
                    if df_cropsar_field_window.isnull().values.any() or df_cropsar_field_window.isnull().values.any():
                        continue
                    prediction_date_window = pd.DataFrame(
                        df_cropsar_field_window.index[0] + timedelta(window_width / 2),index=[str(id) + '_{}_{}'.format(ro_interest, str(p))], columns=(['prediction_date_window']))  # the center data of the window which can is in fact the harvest prediction date if the model returns 1
                    df_cropcalendar_dates_dataset = dict_cropcalendars_data.get(dataset + '_'+crop_calendar_events[0].lower()) #dataframe with the crop calendar observed dates for the specific dataset
                    harvest_date_field = pd.DataFrame([df_cropcalendar_dates_dataset.loc[df_cropcalendar_dates_dataset.id == str(id)][crop_calendar_events[0].lower()].values[0]],
                                                      index=[str(id) + '_{}_{}'.format(ro_interest, str(p))],
                                                      columns=[crop_calendar_events[0].lower()])
                    df_cropsar_field_window = pd.DataFrame(df_cropsar_field_window.T.values,
                                                           index=[str(id) + '_{}_{}'.format(ro_interest, str(p))],
                                                           columns=(['fAPAR_{}'.format(n) for n in
                                                                     range(1, window_values + 1)]))
                    # df_cropsar_field = df_cropsar_field.reset_index()

                    df_VHVV_field_window = pd.DataFrame(df_VHVV_field.iloc[p:p + window_values, 0])
                    if df_VHVV_field_window.isnull().values.any() or df_VHVV_field_window.isna().values.any():
                        continue
                    df_VHVV_field_window.index.name = 'Date'
                    df_dates_window = pd.DataFrame(df_VHVV_field_window.index)
                    df_dates_window = pd.DataFrame(df_dates_window.T.values,
                                                   index=[str(id) + '_{}_{}'.format(ro_interest, str(p))], columns=(
                        ['Date_{}'.format(n) for n in range(1, window_values + 1)]))
                    if pd.isnull(df_cropcalendar_dates_dataset[df_cropcalendar_dates_dataset.id == str(id)][crop_calendar_events[0].lower()].values[0]):
                        continue
                    datetime_crop_event = pd.to_datetime(df_cropcalendar_dates_dataset.loc[df_cropcalendar_dates_dataset.id == str(id)]['{}'.format(crop_calendar_events[0].lower())].values[0])
                    datetime_crop_event = datetime_crop_event.tz_localize(None)
                    df_diff_harvest_window = pd.DataFrame(df_VHVV_field_window.index - datetime_crop_event
                                                          )
                    df_diff_harvest_window = pd.DataFrame(df_diff_harvest_window.T.values,
                                                          index=[str(id) + '_{}_{}'.format(ro_interest, str(p))],
                                                          columns=(['Diff_harvest_{}'.format(n) for n in
                                                                    range(1, window_values + 1)]))
                    df_VHVV_field_window = pd.DataFrame(df_VHVV_field_window.T.values,
                                                        index=[str(id) + '_{}_{}'.format(ro_interest, str(p))], columns=(
                        ['ro_{}_VHVV_{}'.format('_'.join(item[2:] for item in ROs_dataset_dict.get(dataset)),n) for n in range(1, window_values + 1)]))
                    df_window_field_concat_var = pd.concat(
                        [df_cropsar_field_window, df_VHVV_field_window, df_diff_harvest_window, df_dates_window,
                         prediction_date_window, harvest_date_field], axis=1)
                    df_field_input_data_harvest_model.append(df_window_field_concat_var)
                if df_field_input_data_harvest_model:
                    df_field_input_data_harvest_model = pd.concat(df_field_input_data_harvest_model, axis=0)
                    df_harvest_model.append(df_field_input_data_harvest_model)
            df_harvest_model = pd.concat(df_harvest_model, axis=0)
            df_harvest_model.index.name = 'ID_field'
            df_harvest_model_ro_combined.append(df_harvest_model)
        dict_moving_window_extracts.update({'moving_window_extracts_'+ dataset+ '_'+crop_calendar_events[0].lower(): pd.concat(df_harvest_model_ro_combined)})
    return dict_moving_window_extracts







