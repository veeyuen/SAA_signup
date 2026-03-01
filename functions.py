# streamlit_app.py

import pandas as pd
import streamlit as st
import datetime
import numpy as np
import re
import gcsfs
import gspread
import math
from st_files_connection import FilesConnection



## Helper functions

# Convert results into standard format

#@st.cache_data
def convert_time(i, string, metric):

    global output
    
    l=['discus', 'throw', 'jump', 'vault', 'shot', 'javelin']
        
    string=string.lower()

    output=''
    
   # print('metric', metric)
    
    try:
        
        if 'w' in metric:  # skip marks with illegal wind speeds
            
     #       print('W', metric)
            
            output=''
            
        else:
            
    
            if any(s in string for s in l)==True:
            
                if 'm' in metric:
            
                    metric=metric.replace('m', '')
                    output=float(str(metric))
            
                elif 'GR' in metric:
            
                    metric=metric.replace('GR', '')
                    output=float(str(metric))
                
                
                else:
    
                    output=float(str(metric))

            elif string=='':   # no event description at all!
                
                output='' # return nothing
            
        
            else:
        
                searchstring = ":"
                searchstring2 = "."
                substring=str(metric)
                count = substring.count(searchstring)
                count2 = substring.count(searchstring2)
            
                if count==0:
                
                    output=float(substring)

                elif '10,000m' in string and count==2:  # fix erroneous timing format from XX:XX:XX to XX:XX.XX
                
                
                    idx = 5 # 6th character position
                    replacement = "."
                    metric = metric[:idx] + replacement + metric[idx+1:]                
                
                    m,s = metric.split(':')            

                    output = float(datetime.timedelta(minutes=int(m),seconds=float(s)).total_seconds())

                elif '5000m' in string and count==2:  # fix erroneous timing format from XX:XX:XX to XX:XX.XX
                
                
                    idx = 5 # 6th character position
                    replacement = "."
                    metric = metric[:idx] + replacement + metric[idx+1:]                
                
                    m,s = metric.split(':')            

                    output = float(datetime.timedelta(minutes=int(m),seconds=float(s)).total_seconds())          

                elif '1500m' in string and count==2:  # fix erroneous timing format from XX:XX:XX to XX:XX.XX
                    
                    if len(substring)==7:  # format is X:XX:XX and not XX:XX:XX 
                        
                        idx = 4 # 5th character position
                        replacement = "."
                        metric = '0' + metric[:idx] + replacement + metric[idx+1:]                
                
                        m,s = metric.split(':')            

                        output = float(datetime.timedelta(minutes=int(m),seconds=float(s)).total_seconds())
                    
                        
                    else:  # format is XX:XX:XX
                        
                        idx = 5 # 5th character position
                        replacement = "."
                        metric = metric[:idx] + replacement + metric[idx+1:]                
                
                        m,s = metric.split(':')            

                        output = float(datetime.timedelta(minutes=int(m),seconds=float(s)).total_seconds())
                       
                       
                elif (type(metric)==datetime.time or type(metric)==datetime.datetime):
                
                                                
                    time=str(metric)
                    h, m ,s = time.split(':')
                    output = float(datetime.timedelta(hours=int(h),minutes=int(m),seconds=float(s)).total_seconds())
            
                                
                elif (count==1 and count2==1):
            
                    m,s = metric.split(':')
                    output = float(datetime.timedelta(minutes=int(m),seconds=float(s)).total_seconds())
                     
                elif (count==1 and count2==2):
                
            
                    metric = metric.replace(".", ":", 1)
            
                    h,m,s = metric.split(':')            
                    output = float(datetime.timedelta(hours=int(h),minutes=int(m),seconds=float(s)).total_seconds())
                
        
                elif (count==2 and count2==0):
                
            
                    h,m,s = metric.split(':')
                    output = float(datetime.timedelta(hours=int(h),minutes=int(m),seconds=float(s)).total_seconds())
  
    except:
        
        pass
                
    return output


## Convert time into seconds and distances into float

#@st.cache_data
def process_results(df):

    df.reset_index(drop=True, inplace=True)

    for col in df.columns:
    
        df[col] = df[col].astype(str)
        df[col] = df[col].str.replace('\xa0', ' ', regex=True)
        df[col] = df[col].str.replace('[\x00-\x1f\x7f-\x9f]', '', regex=True)
        df[col] = df[col].str.replace('\r', ' ', regex=True)
        df[col] = df[col].str.replace('\n', ' ', regex=True)
        df[col] = df[col].str.strip()

 
    # Define a filter for rows with convertible results
    invalid_results = {'—', 'None', 'DQ', 'SCR', 'FS', 'DNQ', 'DNS', 'NH', 'NM', 'FOUL', 'DNF', 'SR'}

# Apply conversion vectorized using apply, skipping invalid values
    def convert_for_row(row):
        if row['RESULT'] in invalid_results:
            return ''
        return convert_time_refactored(row.name, row['MAPPED_EVENT'], row['RESULT'])

    df['RESULT_CONV'] = df.apply(convert_for_row, axis=1)

        
    #df[['RESULT_CONV']] = df[['RESULT_CONV']].apply(pd.to_numeric)

#    st.write(df.columns)
    
  #  mask = df['CATEGORY_EVENT'].str.contains(r'Jump|Throw|jump|throw|Decathlon|Heptathlon|decathlon|heptathlon', na=True)
    mask = df['CATEGORY_EVENT'].str.lower().str.contains(r'jump|throw|decathlon|heptathlon', na=True)
    
    df[['2%', '3.50%', '5%', '10%', 'RESULT_CONV', 'STANDARDISED_BENCHMARK']] = df[['2%', '3.50%', '5%', '10%', 'RESULT_CONV', 'STANDARDISED_BENCHMARK']].apply(pd.to_numeric, errors='coerce')


    df.loc[mask, 'Delta2'] = df['RESULT_CONV']-df['2%']
    df.loc[mask, 'Delta3.5'] = df['RESULT_CONV']-df['3.50%']
    df.loc[mask, 'Delta5'] = df['RESULT_CONV']-df['5%']
    df.loc[mask, 'Delta10'] = df['RESULT_CONV']-df['10%']
    df.loc[mask, 'Delta_Benchmark'] = df['RESULT_CONV']-df['STANDARDISED_BENCHMARK']
    
    df.loc[~mask, 'Delta2'] =  df['2%'] - df['RESULT_CONV']
    df.loc[~mask, 'Delta3.5'] = df['3.50%'] - df['RESULT_CONV']
    df.loc[~mask, 'Delta5'] = df['5%'] - df['RESULT_CONV']
    df.loc[~mask, 'Delta10'] = df['10%'] - df['RESULT_CONV']
    df.loc[~mask, 'Delta_Benchmark'] = df['STANDARDISED_BENCHMARK'] - df['RESULT_CONV']

  #  df=df.loc[df['COMPETITION']!='Southeast Asian Games'] # Do not include results from SEAG in dataset
         
    return df

#@st.cache_data
def map_international_events(athletes):

    # Create temporary mapped event column

   # athletes['MAPPED_EVENT']=''

## Clear columns of special characters and spaces

    for col in athletes.columns:
        athletes[col] = athletes[col].astype(str)
        athletes[col] = athletes[col].str.replace('\xa0', ' ', regex=True)
        athletes[col] = athletes[col].str.replace('[\x00-\x1f\x7f-\x9f]', '', regex=True)
        athletes[col] = athletes[col].str.replace('\r', ' ', regex=True)
        athletes[col] = athletes[col].str.replace('\n', ' ', regex=True)
        athletes[col] = athletes[col].str.strip()

    # Correct javelin category
    
    mask = athletes['EVENT'].str.contains(r'Javelin', na=True)
    athletes.loc[mask, 'CATEGORY_EVENT'] = 'Throw'
    
    
    # Running

    mask = (athletes['EVENT'].str.contains(r'Dash', na=True) & athletes['DISTANCE'].str.contains(r'100', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '100m'
    mask = (athletes['EVENT'].str.contains(r'Run', na=True) & athletes['DISTANCE'].str.contains(r'100', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '100m'
    mask = athletes['EVENT'].str.contains(r'100 Meter Run', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '100m'
    mask = athletes['EVENT'].str.contains(r'^100m$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '100m'
    
    mask = (athletes['EVENT'].str.contains(r'Dash', na=True) & athletes['DISTANCE'].str.contains(r'200', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '200m'
    mask = (athletes['EVENT'].str.contains(r'Run', na=True) & athletes['DISTANCE'].str.contains(r'200', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '200m'
    mask = athletes['EVENT'].str.contains(r'^200m$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '200m'
    mask = athletes['EVENT'].str.contains(r'200\sMeter', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '200m'
    
    mask = (athletes['EVENT'].str.contains(r'Dash', na=True) & athletes['DISTANCE'].str.contains(r'400', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m'
    mask = athletes['EVENT'].str.contains(r'^400m$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m'
    mask = athletes['EVENT'].str.contains(r'^400\sMeter$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m'
    mask = (athletes['EVENT'].str.contains(r'Run', na=True) & athletes['DISTANCE'].str.contains(r'400', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m'
    
    
    mask800 = (athletes['EVENT'].str.contains(r'Run', na=True) & athletes['DISTANCE'].str.contains(r'800', na=True))
    athletes.loc[mask800, 'MAPPED_EVENT'] = '800m'
    mask = athletes['EVENT'].str.contains(r'800 Meter Run', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '800m'
    mask = athletes['EVENT'].str.contains(r'^800m$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '800m'
    mask = (athletes['EVENT'].str.contains(r'Run', na=True) & athletes['DISTANCE'].str.contains(r'1000', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '1000m'

    
    
    mask = (athletes['EVENT'].str.contains(r'Run', na=True) & athletes['DISTANCE'].str.contains(r'1500', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '1500m'
    mask = athletes['EVENT'].str.contains(r'^1500m$', na=True, regex=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '1500m'
    mask = (athletes['EVENT'].str.contains(r'Run', na=True) & athletes['DISTANCE'].str.contains(r'3000', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '3000m'
    #mask = athletes['EVENT'].str.contains(r'3000m', na=True)
    #athletes.loc[mask, 'MAPPED_EVENT'] = '3000m'
    mask = (athletes['EVENT'].str.contains(r'Run', na=True) & athletes['DISTANCE'].str.contains(r'5000', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '5000m'
    mask = athletes['EVENT'].str.contains(r'^5000m$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '5000m'
    mask = (athletes['EVENT'].str.contains(r'Run', na=True) & athletes['DISTANCE'].str.contains(r'10000', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '10,000m'
    mask = athletes['EVENT'].str.contains(r'^10000m$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '10,000m'
    mask = athletes['EVENT'].str.contains(r'^10\,000m$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '10,000m'
    mask = (athletes['EVENT'].str.contains(r'Run', na=True) & athletes['DISTANCE'].str.contains(r'Mile', na=True))
    athletes.loc[mask, 'MAPPED_EVENT'] = '1 Mile'
    
    
    # Hurdles
    
    
    
    mask = (athletes['EVENT'].str.contains(r'100m Hurdles|100m hurdles', na=False) & athletes['EVENT_CLASS'].str.contains('0.84|0.838|83.8', na=False) & athletes['GENDER'].str.contains(r'Female', na=False))  # this is the correct syntax
    athletes.loc[mask, 'MAPPED_EVENT'] = '100m Hurdles'
    mask = (athletes['EVENT'].str.contains(r'100m Hurdles|100m hurdles', na=False) & athletes['DIVISION'].str.contains('None', na=False) & athletes['GENDER'].str.contains(r'Female', na=False) & athletes['REGION'].str.contains(r'International', na=False))  # this is the correct syntax
    athletes.loc[mask, 'MAPPED_EVENT'] = '100m Hurdles'
    mask = (athletes['EVENT'].str.contains(r'^Hurdles$', na=False) & athletes['DISTANCE'].str.contains(r'100', na=False) & athletes['DIVISION'].str.contains(r'OPEN|Open', na=False) & athletes['GENDER'].str.contains(r'Female', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '100m Hurdles'
    mask = (athletes['EVENT'].str.contains(r'100m Hurdles|100m hurdles', na=False) & athletes['REGION'].str.contains(r'International', na=False) & athletes['GENDER'].str.contains(r'Female', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '100m Hurdles'
    mask = (athletes['EVENT'].str.contains(r'^Hurdles$', na=False) & athletes['DISTANCE'].str.contains(r'100', na=False) & athletes['EVENT_CLASS'].str.contains(r'0.838|0.84|83.8', na=False) & athletes['GENDER'].str.contains(r'Female', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '100m Hurdles'
    
    # 110m Hurdles
    
    mask = (athletes['EVENT'].str.contains(r'110m Hurdles|110m hurdles', na=False) & (athletes['EVENT_CLASS'].isna() | # 1. Check for True NaN/NaT values
     (athletes['EVENT_CLASS'].astype(str).str.strip() == '') | # 2. Check for empty string or only whitespace
     (athletes['EVENT_CLASS'].astype(str).str.lower().str.contains(r'none|nan', na=False)) # 3. Check for string representations
    ) & athletes['GENDER'].str.contains(r'Male', na=False) & athletes['REGION'].str.contains(r'International', na=False))  # this is the correct syntax
    athletes.loc[mask, 'MAPPED_EVENT'] = '110m Hurdles'

    mask = (athletes['EVENT'].str.contains(r'^Hurdles$', na=False) & athletes['DISTANCE'].str.contains(r'110', na=False) & athletes['DIVISION'].str.contains(r'OPEN|Open', na=False) & athletes['GENDER'].str.contains(r'Male', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '110m Hurdles'
    mask = (athletes['EVENT'].str.contains(r'^Hurdles$', na=False) & athletes['DISTANCE'].str.contains(r'110', na=False) & athletes['EVENT_CLASS'].str.contains(r'1.067', na=False) & athletes['GENDER'].str.contains(r'Male', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '110m Hurdles'
    mask = ((athletes['EVENT'].str.contains(r'110m Hurdles|110m hurdles', na=False)) 
             & ((athletes['EVENT_CLASS'].str.contains('None', na=False))|(athletes['EVENT_CLASS']==np.nan)|(athletes['EVENT_CLASS']=='')) 
             & athletes['REGION'].str.contains(r'International', na=False) & (athletes['DIVISION'].str.contains(r'None', na=False)))  # this is the correct syntax
    athletes.loc[mask, 'MAPPED_EVENT'] = '110m Hurdles'
    
    
    # Using np.where instead
    # 110m hurdles 1.067m male
    # 100m hurdles 0.838m female
    # 400m hurdles 0.914m male
    # 400m hurdles 0.762m female
    
                                    
    
    
    mask = (athletes['EVENT'].str.contains(r'^Hurdles$', na=False) & athletes['DISTANCE'].str.contains(r'110', na=False) & athletes['EVENT_CLASS'].str.contains(r'1.067', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '110m Hurdles'
    mask = (athletes['EVENT'].str.contains(r'^Hurdles$', na=False) & athletes['DISTANCE'].str.contains(r'400', na=False) & athletes['EVENT_CLASS'].str.contains(r'0.762|76.2cm', na=False) & athletes['GENDER'].str.contains(r'Female', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m Hurdles'
    
    
    mask = (athletes['EVENT'].str.contains(r'^Hurdles$', na=False) & athletes['DISTANCE'].str.contains(r'400', na=False) & athletes['EVENT_CLASS'].str.contains(r'0.914', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m Hurdles'
    mask = (athletes['EVENT'].str.contains(r'^Hurdles$', na=False) & athletes['DISTANCE'].str.contains(r'400', na=False) & athletes['DIVISION'].str.contains(r'Open|Invitational', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m Hurdles'
    
    mask = (athletes['EVENT'].str.contains(r'400m Hurdles', na=False) & athletes['EVENT_CLASS'].str.contains(r'0.914', na=False)  & athletes['GENDER'].str.contains(r'Male', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m Hurdles'
    
    
    mask = (athletes['EVENT'].str.contains(r'Hurdles', na=False) & athletes['DISTANCE'].str.contains(r'400', na=False) & athletes['EVENT_CLASS'].str.contains(r'0.762', na=False)& athletes['GENDER'].str.contains(r'Female', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m Hurdles'
    mask = (athletes['EVENT'].str.contains(r'400m Hurdles', na=False) & athletes['EVENT_CLASS'].str.contains(r'0.762m', na=False) & athletes['GENDER'].str.contains(r'Female', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m Hurdles'
    mask = (athletes['EVENT'].str.contains(r'400m Hurdles|400m hurdles', na=False) & athletes['EVENT_CLASS'].str.contains('None|0.762|0.914', na=False) & athletes['REGION'].str.contains(r'International', na=False))  # this is the correct syntax
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m Hurdles'
    mask = (athletes['EVENT'].str.contains(r'400m Hurdles|400m hurdles', na=False) & athletes['REGION'].str.contains(r'International', na=False))  # this is the correct syntax
    athletes.loc[mask, 'MAPPED_EVENT'] = '400m Hurdles'
    
    
    
    # Throws
    
    
    
    mask = (athletes['EVENT'].str.contains(r'Javelin Throw|Javelin throw|Javelin', na=False) & athletes['EVENT_CLASS'].str.contains(r'600', na=False) & athletes['GENDER'].str.contains(r'Female', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Javelin Throw'
    mask = (athletes['EVENT'].str.contains(r'Javelin Throw|Javelin throw|Javelin', na=False) & athletes['EVENT_CLASS'].str.contains(r'800', na=False) & athletes['GENDER'].str.contains(r'Male', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Javelin Throw'
    mask = (athletes['EVENT'].str.contains(r'Javelin Throw|Javelin throw', na=False) & athletes['DIVISION'].str.contains(r'OPEN|Open', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Javelin Throw'
    
    mask = (athletes['EVENT'].str.contains(r'Shot Put|Shot put', na=False, regex=True) & (athletes['GENDER']=='Female') & (athletes['EVENT_CLASS']=='4kg'))# there are some additional characters after Put
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Shot Put'
    
    
    mask = (athletes['EVENT'].str.contains(r'Shot Put|Shot put', na=False) & (athletes['GENDER']=='Male') & (athletes['EVENT_CLASS'].str.contains(r'7.26', na=False)))# there are some additional characters after Put
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Shot Put'
    mask = (athletes['EVENT'].str.contains(r'Shot Put|Shot put', na=False) & (athletes['GENDER']=='Female') & (athletes['EVENT_CLASS'].str.contains(r'4', na=False)))# there are some additional characters after Put
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Shot Put'
    
    mask = (athletes['EVENT'].str.contains(r'Shot Put|Shot put', na=False) & (athletes['DIVISION'].str.contains(r'OPEN|Open', na=False)))# there are some additional characters after Put
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Shot Put'
    
    mask = (athletes['EVENT'].str.contains(r'Shot Put|Shot put', na=False) & (athletes['REGION'].str.contains(r'International', na=False)) & athletes['EVENT_CLASS'].str.contains(r'None|nan', na=False))# there are some additional characters after Put
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Shot Put'
    
    
    
    mask = (athletes['EVENT'].str.contains(r'Hammer Throw|Hammer throw', na=False) & athletes['EVENT_CLASS'].str.contains(r'7.26', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Hammer Throw'
    mask = (athletes['EVENT'].str.contains(r'Hammer Throw|Hammer throw', na=False) & athletes['EVENT_CLASS'].str.contains(r'4', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Hammer Throw'
    mask = (athletes['EVENT'].str.contains(r'Hammer Throw|Hammer throw', na=False) & (athletes['DIVISION'].str.contains(r'OPEN|Open', na=False)))# there are some additional characters after Put
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Hammer Throw'
    
    mask = (athletes['EVENT'].str.contains(r'Discus Throw|Discus|Discus throw', na=False) & athletes['EVENT_CLASS'].str.contains(r'2', na=False) & athletes['GENDER'].str.contains(r'Male', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Discus Throw'
    mask = (athletes['EVENT'].str.contains(r'Discus Throw|Discus|Discus throw', na=False) & athletes['EVENT_CLASS'].str.contains(r'1', na=False) & athletes['GENDER'].str.contains(r'Female', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Discus Throw'
    
    mask = (athletes['EVENT'].str.contains(r'Discus Throw|Discus throw', na=False) & athletes['DIVISION'].str.contains(r'OPEN|Open', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Discus Throw'
    mask = (athletes['EVENT'].str.contains(r'Discus Throw|Discus throw', na=False) & athletes['DIVISION'].str.contains(r'None', na=False) & athletes['EVENT_CLASS'].str.contains(r'None', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Discus Throw'
    
    mask = (athletes['EVENT'].str.contains(r'Discus Throw|Discus throw', na=False) & athletes['REGION'].str.contains(r'International', na=False) & (athletes['EVENT_CLASS'].str.contains(r'', na=False)|athletes['EVENT_CLASS'].str.contains(r'nan|None', na=False))) 
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Discus Throw'
    
    
    
    # Jumps
    
    mask = athletes['EVENT'].str.contains(r'High Jump', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'High Jump'
    
    mask = athletes['EVENT'].str.contains(r'^Long\sJump$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Long Jump'
    mask = athletes['EVENT'].str.contains(r'Long Jump Open', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Long Jump'
    mask = athletes['EVENT'].str.contains(r'Long Jump Trial', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Long Jump'
    
    
    mask = athletes['EVENT'].str.contains(r'Triple Jump', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Triple Jump'
    mask = athletes['EVENT'].str.contains(r'Pole Vault', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Pole Vault'
    mask = athletes['EVENT'].str.contains(r'High jump', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'High Jump'
    mask = athletes['EVENT'].str.contains(r'Long jump', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Long Jump'
    mask = athletes['EVENT'].str.contains(r'Triple jump', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Triple Jump'
    mask = athletes['EVENT'].str.contains(r'^Pole\svault$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Pole Vault'
    
    # Steeplechase
    
    mask = (athletes['EVENT'].str.contains(r'3000m Steeplechase|3000m S\/C', na=True) & athletes['REGION'].str.contains(r'International', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '3000m Steeplechase'
    mask = (athletes['EVENT'].str.contains(r'Steeplechase|S\/C', na=False) & athletes['DISTANCE'].str.contains(r'3000', na=False)  & athletes['EVENT_CLASS'].str.contains(r'0.914', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '3000m Steeplechase'
    mask = (athletes['EVENT'].str.contains(r'3000m Steeplechase|3000m S\/C', na=False) & athletes['EVENT_CLASS'].str.contains(r'0.914', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '3000m Steeplechase'
    mask = (athletes['EVENT'].str.contains(r'Steeplechase', na=False) & athletes['DISTANCE'].str.contains(r'3000', na=False)  & athletes['DIVISION'].str.contains(r'OPEN|Open', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '3000m Steeplechase'
    
    
    # Marathon
    
    mask = athletes['EVENT'].str.contains(r'^Marathon$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Marathon'
    mask = athletes['EVENT'].str.contains(r'^Half\sMarathon$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Half Marathon'
    mask = athletes['EVENT'].str.contains(r'^Half\smarathon$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Half Marathon'
    
    
    # Walk
    
    mask = (athletes['EVENT'].str.contains(r'Race Walk', na=False) & athletes['DISTANCE'].str.contains(r'10000', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '10000m Racewalk'
    
    
    
    # Relay
    
    mask = athletes['EVENT'].str.contains(r'4x80m Relay', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 80m'
    mask = athletes['EVENT'].str.contains(r'^4\sx\s100m$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 100m'
    mask = athletes['EVENT'].str.contains(r'4x100m Relay', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 100m'
    mask = athletes['EVENT'].str.contains(r'4 X 100m Relay', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 100m'
    mask = athletes['EVENT'].str.contains(r'4 x 100m Relay', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 100m'
    mask = (athletes['EVENT'].str.contains(r'Relay', na=False) & athletes['DISTANCE'].str.contains(r'400', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 100m'

    mask = athletes['EVENT'].str.contains(r'4x400m Relay', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 400m'
    mask = athletes['EVENT'].str.contains(r'4 X 400m Relay', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 400m'
    mask = athletes['EVENT'].str.contains(r'4x100 Meter Relay', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 100m'
    mask = athletes['EVENT'].str.contains(r'4 x 100m Relay', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 100m'
    mask = (athletes['EVENT'].str.contains(r'Relay', na=False) & athletes['DISTANCE'].str.contains(r'1600', na=False))
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 400m'
    mask = athletes['EVENT'].str.contains(r'^4\sx\s400m$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = '4 x 400m'

    # Decathlon/Heptathlon
    
    mask = athletes['EVENT'].str.contains(r'^Heptathlon$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Heptathlon'
    mask = athletes['EVENT'].str.contains(r'^Decathlon$', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Decathlon'
    mask = athletes['EVENT'].str.contains(r'Heptathlon', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Heptathlon'
    mask = athletes['EVENT'].str.contains(r'Decathlon', na=True)
    athletes.loc[mask, 'MAPPED_EVENT'] = 'Decathlon'

    return athletes


#@st.cache_data
def revert_times(df):

    df.reset_index(drop=True, inplace=True)

    for i in range(len(df)):
            
        rowIndex = df.index[i]
    
        event=df.loc[rowIndex,'MAPPED_EVENT']
            
        
        time_base2=df.loc[rowIndex,'2%']
        time_base3=df.loc[rowIndex,'3.5%']
        time_base5=df.loc[rowIndex,'5%']
        
            
        if metric==None:
            continue
            
        if event=='800m' or event=='10,000m' or event=='5000m' or event=='3000m Steeplechase' or event=='1500m':
            
          #  print(i, event, time_base2, time_base3, time_base5)     
            
            date_preconvert2 = datetime.datetime.utcfromtimestamp(time_base2)
            date_preconvert3 = datetime.datetime.utcfromtimestamp(time_base3)
            date_preconvert5 = datetime.datetime.utcfromtimestamp(time_base5)
            
        #    print(date_preconvert2, date_preconvert3, date_preconvert5)
                
            
            output2 = datetime.datetime.strftime(date_preconvert2, "%M:%S.%f")
            output3 = datetime.datetime.strftime(date_preconvert3, "%M:%S.%f")
            output5 = datetime.datetime.strftime(date_preconvert5, "%M:%S.%f")
                
         #   print(event, output2, output3, output5)
            
       
            df.at[rowIndex, '2%'] = output2 # copy over time format
            df.at[rowIndex, '3.5%'] = output3
            df.at[rowIndex, '5%'] = output5
    
            
        elif event=='Marathon':
            
          #  print(time_base2, time_base3, time_base5)
    
            
            try:
                
    
            
                date_preconvert2 = datetime.datetime.utcfromtimestamp(time_base2)
                date_preconvert3 = datetime.datetime.utcfromtimestamp(time_base3)
                date_preconvert5 = datetime.datetime.utcfromtimestamp(time_base5)
    
                
                
                output2 = datetime.datetime.strftime(date_preconvert2, "%H:%M:%S")
                output3 = datetime.datetime.strftime(date_preconvert3, "%H:%M:%S")
                output5 = datetime.datetime.strftime(date_preconvert5, "%H:%M:%S")
    
                
                df.at[rowIndex, '2%'] = output2 # copy over time format
                df.at[rowIndex, '3.5%'] = output3
                df.at[rowIndex, '5%'] = output5
    
                
             #   print('output', output2, output3, output5)
    
    
            
            except:
                
                pass

    return df

#@st.cache_data
#def clean_columns(df):

 #   for col in df.columns:
#        df[col] = df[col].astype(str)
#        df[col] = df[col].str.replace('\xa0', ' ', regex=True)
#        df[col] = df[col].str.replace('[\x00-\x1f\x7f-\x9f]', '', regex=True)
#        df[col] = df[col].str.replace('\r', ' ', regex=True)
#        df[col] = df[col].str.replace('\n', ' ', regex=True)
#        df[col] = df[col].str.strip()

#    return df

def clean_columns(df):
    # Only apply string cleaning to object columns
    str_cols = df.select_dtypes(include='object').columns
    for col in str_cols:
        df[col] = (df[col]
            .str.replace('\xa0', ' ', regex=True)
            .str.replace('[\x00-\x1f\x7f-\x9f]', '', regex=True)
            .str.replace('\r', ' ', regex=True)
            .str.replace('\n', ' ', regex=True)
            .str.strip()
        )
    return df

def convert_time_refactored(i, string, metric):
    """
    Convert various metric formats (distance, time) to a float value (primarily seconds for times).
    Optimized for speed: no global variables, no print statements, no unnecessary conversions.

    Args:
        i (int): Index (unused, kept for compatibility).
        string (str): Event description.
        metric (str, float, or datetime): The result metric.

    Returns:
        float or empty string: Converted metric as float (seconds/meters), or '' if not convertible.
    """
    l = ['discus', 'throw', 'jump', 'vault', 'shot']
    string = str(string).lower()
    metric_str = str(metric)
    output = ''
         
    
    try:
        # Skip marks with illegal wind speeds
        if isinstance(metric_str, str) and 'w' in metric_str.lower():
            # Remove 'w' (case-insensitive) and any leading/trailing whitespace
            metric_str = metric_str.lower().replace('w', '').strip()  # NEW

            result_float = float(metric_str)
    
            return f"{result_float:.2f}"
       #     return float(metric_str)  # NEW
       #     return ''

        # Field events (distances)
        if any(s in string for s in l):
            # Remove unit if present
            metric_clean = metric_str.replace('m', '').replace('GR', '')
            result_clean = float(metric_clean)
            return f"{result_clean:.2f}"


        # No event description
        if string == '':
            return ''

        # Time events
        count_colon = metric_str.count(':')
        count_dot = metric_str.count('.')

        # Simple time as float (no colon)
        if count_colon == 0:

            result_metric = float(metric_str)
            return f"{result_metric:.2f}"


        #    return float(metric_str)

        # HH:MM:SS.sss format (2 colons, 1 or more dots) - FIX
        if count_colon == 2 and count_dot >= 1:
            h, m, s_with_ms = metric_str.split(':')
            return float(int(h) * 3600 + int(m) * 60 + float(s_with_ms))
        
        # Convert time formats with two colons (like XX:XX:XX, XX:XX.XX)
        if count_colon == 2:

            # Check if this is a standard HH:MM:SS (no decimal point)
            if count_dot == 0:  # NEW section to capture HH:MM:SS with no decimal point
                h, m, s = metric_str.split(':')
                return float(
                    int(h) * 3600 + int(m) * 60 + float(s)
                )
                
            # For 10,000m and 5000m, replace the 6th character with '.' for format XX:XX.XX
            if ('10,000m' in string or '5000m' in string or '1500m' in string):
                if len(metric_str) == 7:  # X:XX:XX (1500m special case)
                    idx = 4
                    metric_mod = '0' + metric_str[:idx] + '.' + metric_str[idx+1:]
                else:
                    idx = 5
                    metric_mod = metric_str[:idx] + '.' + metric_str[idx+1:]
                m, s = metric_mod.split(':')[-2:]
                return float(
                    (int(m) * 60) + float(s)
                )

            # Standard HH:MM:SS
            h, m, s = metric_str.split(':')
            return float(
                int(h) * 3600 + int(m) * 60 + float(s)
            )

        # Handle datetime.time/datetime.datetime objects
        if isinstance(metric, (datetime.time, datetime.datetime)):
            t = str(metric)
            h, m, s = t.split(':')
            return float(int(h) * 3600 + int(m) * 60 + float(s))

        # MM:SS.sss format
        if count_colon == 1 and count_dot >= 1:
            m, s = metric_str.split(':')
            return float(int(m) * 60 + float(s))

        # HH.MM.SS (rare) or MM:SS:SS
        if count_colon == 1 and count_dot == 2:
            # Replace first dot with colon
            metric_mod = metric_str.replace('.', ':', 1)
            h, m, s = metric_mod.split(':')
            return float(int(h) * 3600 + int(m) * 60 + float(s))

        # HH:MM:SS (no dots)
        if count_colon == 2 and count_dot == 0:
            h, m, s = metric_str.split(':')
            return float(int(h) * 3600 + int(m) * 60 + float(s))

        # MM:SS (no dots)
        if count_colon == 1 and count_dot == 0:
            m, s = metric_str.split(':')
            return float(int(m) * 60 + int(s))

    except Exception:
        return ''

    return output
   # return format_seconds_to_time_string(total_seconds, is_long_event=False) # Example usage

def process_results_refactored(df):
    
    df.reset_index(drop=True, inplace=True)
    df = clean_columns(df)
    skip_results = {'—', 'DQ', 'SCR', 'FS', 'DNQ', 'DNS', 'NH', 'NM', 'FOUL', 'DNF', 'SR'}
    mask = ~df['RESULT'].isin(skip_results)
    df.loc[mask, 'RESULT_CONV'] = df[mask].apply(convert_time, axis=1)
    mask_field = df['CATEGORY_EVENT'].str.contains(r'Jump|Throw|jump|throw|Decathlon|Heptathlon|decathlon|heptathlon', na=True)
    num_cols = ['2%', '3.50%', '5%', '10%', 'RESULT_CONV', 'STANDARDISED_BENCHMARK']
    df[num_cols] = df[num_cols].apply(pd.to_numeric, errors='coerce')
    df.loc[mask_field, 'Delta2'] = df['RESULT_CONV']-df['2%']
    df.loc[mask_field, 'Delta3.5'] = df['RESULT_CONV']-df['3.50%']
    df.loc[mask_field, 'Delta5'] = df['RESULT_CONV']-df['5%']
    df.loc[mask_field, 'Delta10'] = df['RESULT_CONV']-df['10%']
    df.loc[mask_field, 'Delta_Benchmark'] = df['RESULT_CONV']-df['STANDARDISED_BENCHMARK']
    df.loc[~mask_field, 'Delta2'] =  df['2%'] - df['RESULT_CONV']
    df.loc[~mask_field, 'Delta3.5'] = df['3.50%'] - df['RESULT_CONV']
    df.loc[~mask_field, 'Delta5'] = df['5%'] - df['RESULT_CONV']
    df.loc[~mask_field, 'Delta10'] = df['10%'] - df['RESULT_CONV']
    df.loc[~mask_field, 'Delta_Benchmark'] = df['STANDARDISED_BENCHMARK'] - df['RESULT_CONV']
    return df


def simple_map_events(athletes: pd.DataFrame) -> pd.DataFrame:
    # Columns we care about
    str_cols = ['EVENT', 'DISTANCE']
    existing_cols = [c for c in str_cols if c in athletes.columns]

    # Clean text columns
    regex_cleanup = re.compile(r'[\xa0\r\n]|[\x00-\x1f\x7f-\x9f]')
    for col in existing_cols:
        athletes[col] = (
            athletes[col]
            .astype(str)
            .str.replace(regex_cleanup, ' ', regex=True)
            .str.strip()
        )

    # Initialize mapped column
    if 'MAPPED_EVENT' not in athletes.columns:
        athletes['MAPPED_EVENT'] = np.nan

   
    # ----------------------
    # EVENT-only rules (regex on EVENT)
    # ----------------------
    event_rules = {
        r'(Dash|Run).*\b60\b|60 Meter Run|^60m$': '60m',
        r'(Dash|Run).*\b80\b|80 Meter Run|^80m$': '80m',
        r'(Dash|Run).*\b100\b(?!0)|100 Meter Run\b|^100m$': '100m',
        r'(Dash|Run).*\b200\b|^200m$|200\sMeter': '200m',
        r'(Dash|Run).*\b300\b|^300m$|300\sMeter': '300m',
        r'(Dash|Run).*\b400\b|^400m$|400\sMeter': '400m',
        r'(Run.*800|800 Meter Run|^800m$)': '800m',
        r'(Run.*1000|1000m)\b': '1000m',
        r'(Run.*1500|^1500m$)': '1500m',
        r'(Run.*1600|^1600m$)': '1600m',
        r'(Run.*3000|^3000m$)': '3000m',
        r'(Run.*5000|^5000m$)': '5000m',
        r'(Run.*10,000|Run.*10000|^10,000m$|^10000m$|10km|10 km|10,000 m)': '10,000m',
        r'(Run.*Mile|Mile Run|^Mile$|^1 Mile$)': '1 Mile',  # Enhanced Mile mapping

        # NEW: Half Marathon
        r'Half\s*Marathon|21\.0975\s*km|21\s*km': 'Half Marathon', 
        
        # NEW: Marathon
        r'Marathon|42\.195\s*km|42\s*km': 'Marathon',

        # RELAY RULES
        r'4\s*[xX]\s*100m|4x100\s*Relay|400m\s*Relay': '4 x 100m',
        r'4\s*[xX]\s*400m|4x400\s*Relay|1600m\s*Relay': '4 x 400m',
        r'4\s*[xX]\s*200m|4x200\s*Relay|800m\s*Relay': '4 x 200m',
        }

    for pattern, mapped in event_rules.items():
        athletes.loc[athletes['EVENT'].str.contains(pattern, na=False, case=False), 'MAPPED_EVENT'] = mapped

    # ----------------------
    # EVENT + DISTANCE rules
    # ----------------------
    distance_rules = [
        # Short sprints
        {"conditions": {"EVENT": r'(Dash|Run)', "DISTANCE": r'\b60\b'}, "map_to": "60m"},
        {"conditions": {"EVENT": r'(Dash|Run)', "DISTANCE": r'\b80\b'}, "map_to": "80m"},
        {"conditions": {"EVENT": r'(Dash|Run)', "DISTANCE": r'\b100\b'}, "map_to": "100m"},
        {"conditions": {"EVENT": r'(Dash|Run)', "DISTANCE": r'\b150\b'}, "map_to": "150m"},   
        {"conditions": {"EVENT": r'(Dash|Run)', "DISTANCE": r'\b200\b'}, "map_to": "200m"},
        {"conditions": {"EVENT": r'(Dash|Run)', "DISTANCE": r'\b300\b'}, "map_to": "300m"},
        {"conditions": {"EVENT": r'(Dash|Run)', "DISTANCE": r'\b400\b'}, "map_to": "400m"},
        {"conditions": {"EVENT": r'(Dash|Run)', "DISTANCE": r'\b800\b'}, "map_to": "800m"},
        
        # Middle/long
        {"conditions": {"EVENT": r'Run', "DISTANCE": r'\b1500\b'}, "map_to": "1500m"},
        {"conditions": {"EVENT": r'Run', "DISTANCE": r'\b1600\b'}, "map_to": "1600m"},
        {"conditions": {"EVENT": r'Run', "DISTANCE": r'\b2400\b'}, "map_to": "2400m"},
        {"conditions": {"EVENT": r'Run', "DISTANCE": r'\b3000\b'}, "map_to": "3000m"},
        {"conditions": {"EVENT": r'Run', "DISTANCE": r'\b5000\b'}, "map_to": "5000m"},
        {"conditions": {"EVENT": r'Run', "DISTANCE": r'10000'}, "map_to": "10,000m"},
        {"conditions": {"EVENT": r'10000m'}, "map_to": "10,000m"},

        # Road

        {"conditions": {"EVENT": r'5km, Road'}, "map_to": "5km, Road"},
        
        # Mile - NEW
        {"conditions": {"EVENT": r'(Run|Mile)', "DISTANCE": r'(Mile|1609|1 Mile)'}, "map_to": "1 Mile"},
        
        # Walks
        {"conditions": {"EVENT": r'1500m Race walk'}, "map_to": "1500m Racewalk"},
        {"conditions": {"EVENT": r'(3000m Race walk|3km Racewalk|3km Race Walk)'}, "map_to": "3000m Racewalk"},
        {"conditions": {"EVENT": r'(5000m Race Walk|5km Racewalk)'}, "map_to": "5000m Racewalk"},
        {"conditions": {"EVENT": r'(10km Race Walk|10km Racewalk|10,000m Racewalk)'}, "map_to": "10000m Racewalk"},
        {"conditions": {"EVENT": r'(20km Race Walk|20km Racewalk|20,000m Racewalk)'}, "map_to": "20km Racewalk"},
        {"conditions": {"EVENT": r'Race Walk', "DISTANCE": r'10000'}, "map_to": "10000m Racewalk"},
        
        # Relays
        {"conditions": {"EVENT": r'Relay', "DISTANCE": r'\b400\b'}, "map_to": "4 x 100m"},
        {"conditions": {"EVENT": r'Relay', "DISTANCE": r'\b1600\b'}, "map_to": "4 x 400m"},
        
        # Steeple
        {"conditions": {"EVENT": r'(3000m S/C|3000m SC)'}, "map_to": "3000m Steeplechase"},
        {"conditions": {"EVENT": r'(Steeplechase|S/C|SC)', "DISTANCE": r'3000'}, "map_to": "3000m Steeplechase"},
        {"conditions": {"EVENT": r'(2000m S/C|2000m SC|2000m Steeplechase)'}, "map_to": "2000m Steeplechase"},
        {"conditions": {"EVENT": r'(Steeplechase|S/C|SC)', "DISTANCE": r'2000'}, "map_to": "2000m Steeplechase"},

    ]

    distance_rules.append({
        "conditions": {"EVENT": r'Run|10,000|10000|10km|10 km', "DISTANCE": r'10,000|10000|10km|10 km'},
        "map_to": "10,000m"
    })

    distance_rules.append({
        "conditions": {"EVENT": r'Race Walk|Racewalk', "DISTANCE": r'10,000|10000|10km|10 km'},
        "map_to": "10,000m Racewalk"
    })

    distance_rules.append({
        "conditions": {"EVENT": r'Race Walk|Racewalk', "DISTANCE": r'5000|5km|5 km'},
        "map_to": "5000m Racewalk"
    })

    distance_rules.append({
        "conditions": {"EVENT": r'Race Walk|Racewalk', "DISTANCE": r'3000|3km|3 km'},
        "map_to": "3000m Racewalk"
    })


    
    for rule in distance_rules:
        cond = pd.Series(True, index=athletes.index)
        for col, pat in rule["conditions"].items():
            if col in athletes.columns:
                cond &= athletes[col].str.contains(pat, na=False, case=False, regex=True)
            else:
                cond &= False
        athletes.loc[cond, 'MAPPED_EVENT'] = rule["map_to"]

    # ----------------------
    # Hurdles
    # ----------------------
    hurdles_rules = [
        {"conditions": {"EVENT": r'(60m Hurdles|60m hurdles)'}, "map_to": "60m Hurdles"},
        {"conditions": {"EVENT": r'^Hurdles$', "DISTANCE": r'\b60\b'}, "map_to": "60m Hurdles"},
        {"conditions": {"EVENT": r'(100m Hurdles|100m hurdles)'}, "map_to": "100m Hurdles"},
        {"conditions": {"EVENT": r'^Hurdles$', "DISTANCE": r'\b100\b'}, "map_to": "100m Hurdles"},
        {"conditions": {"EVENT": r'(110m Hurdles|110m hurdles)'}, "map_to": "110m Hurdles"},
        {"conditions": {"EVENT": r'^Hurdles$', "DISTANCE": r'\b110\b'}, "map_to": "110m Hurdles"},
        {"conditions": {"EVENT": r'(200m Hurdles|200m hurdles)'}, "map_to": "200m Hurdles"},
        {"conditions": {"EVENT": r'^Hurdles$', "DISTANCE": r'\b200\b'}, "map_to": "200m Hurdles"},
        {"conditions": {"EVENT": r'(400m Hurdles|400m hurdles)'}, "map_to": "400m Hurdles"},
        {"conditions": {"EVENT": r'^Hurdles$', "DISTANCE": r'\b400\b'}, "map_to": "400m Hurdles"},
    ]

    for rule in hurdles_rules:
        cond = pd.Series(True, index=athletes.index)
        for col, pat in rule["conditions"].items():
            if col in athletes.columns:
                cond &= athletes[col].str.contains(pat, na=False, case=False, regex=True)
            else:
                cond &= False
        athletes.loc[cond, 'MAPPED_EVENT'] = rule["map_to"]

    # ----------------------
    # EVENT-only rules (regex on EVENT) - Field Events (THROWS & JUMPS)
    # ----------------------
    field_event_rules = {
        # Throws
        r'Discus\s*Throw|Discus$': 'Discus Throw',
        r'Shot\s*Put': 'Shot Put',
        r'Javelin\s*Throw|Javelin$': 'Javelin Throw',
        r'Hammer\s*Throw': 'Hammer Throw',

        # Jumps
        r'Long\s*Jump': 'Long Jump',
        r'Triple\s*Jump': 'Triple Jump',
        r'High\s*Jump': 'High Jump',
        r'Pole\s*Vault': 'Pole Vault',

        # Decathlon/Heptathlon
        r'Decathlon': 'Decathlon',
        r'Heptathlon': 'Heptathlon',
        
    }
    
    for pattern, mapped in field_event_rules.items():
        athletes.loc[athletes['EVENT'].str.contains(pattern, na=False, case=False), 'MAPPED_EVENT'] = mapped
   
    return athletes


def normalize_text(s):
    return (str(s).replace('\xa0', '').replace('\r', '').replace('\n', '').strip().casefold())

def normalize_time_format(t):
    """Standardize time strings like '01:54.3' → '01:54.30'."""
    if not isinstance(t, str):
        return t  # leave non-strings unchanged
        
    # Match patterns like mm:ss.s or mm:ss.ss
    match = re.match(r"^(\d{2}):(\d{2})\.(\d{1,2})$", t)
    if match:
        minutes, seconds, fraction = match.groups()
        # Ensure exactly two digits for decimal part
        if len(fraction) == 1:
            fraction += "0"
        # Return standardized string
        return f"{minutes}:{seconds}.{fraction}"
    return t  # Return unchanged if pattern not recognized

def convert_time_format(time_str):
    """
    Convert time from 'HH:MM:SS.mmmmmm' to 'MM:SS.mm' format.
    Also formats float values to ensure 2 decimal places (e.g., 9.1 -> 9.10).
    
    Args:
        time_str: Time string in format 'HH:MM:SS.mmmmmm' or float value
    
    Returns:
        Converted time string in format 'MM:SS.mm' or formatted float with 2 decimals
    """
    if pd.isna(time_str):
        return time_str
    
    time_str = str(time_str)
    
    # Match pattern HH:MM:SS.mmmmmm (with flexible microseconds)
    pattern = r'^(\d{2}):(\d{2}):(\d{2})\.(\d+)$'
    match = re.match(pattern, time_str)
    
    if match:
        hours, minutes, seconds, microseconds = match.groups()
        # Take only first 2 digits of microseconds (centiseconds)
        centiseconds = microseconds[:2].ljust(2, '0')
        return f"{minutes}:{seconds}.{centiseconds}"
    
    # Check if it's a float value (e.g., 9.1, 12.34)
    try:
        float_val = float(time_str)
        return f"{float_val:.2f}"
    except ValueError:
        pass
    
    # Return original if pattern doesn't match and not a float
    return time_str

def convert_time_refactored_2(i, string, metric):
    """
    Convert various metric formats (distance, time) to a float value (primarily seconds for times).
    Optimized for speed: no global variables, no print statements, no unnecessary conversions.
    
    Args:
        i (int): Index (unused, kept for compatibility).
        string (str): Event description.
        metric (str, float, or datetime): The result metric.
    
    Returns:
        float or empty string: Converted metric as float (seconds/meters), or '' if not convertible.
    """
    l = ['discus', 'throw', 'jump', 'vault', 'shot']
    sprint_events = ['100m', '200m', '400m']
    
    string = str(string).lower()
    metric_str = str(metric)
    output = ''
    
    try:
        # Skip marks with illegal wind speeds
        if isinstance(metric_str, str) and 'w' in metric_str.lower():
            return ''
        
        # Field events (distances)
        if any(s in string for s in l):
            # Remove unit if present
            metric_clean = metric_str.replace('m', '').replace('GR', '')
            return round(float(metric_clean), 2)
        
        # No event description
        if string == '':
            return ''
        
        # Time events
        count_colon = metric_str.count(':')
        count_dot = metric_str.count('.')
        
        # Simple time as float (no colon)
        if count_colon == 0:
            return round(float(metric_str), 2)
        
        # Sprint events (100m, 200m, 400m): Handle 00:MM.SS or MM.SS format as seconds
        if any(sprint in string for sprint in sprint_events):
            if count_colon == 1 and count_dot == 1:
                # Format: 00:09.16 or 09.16
                parts = metric_str.split(':')
                if len(parts) == 2:
                    # Check if first part is "00" (ignore it) or actual minutes
                    first_part = parts[0]
                    second_part = parts[1]
                    
                    if first_part == '00':
                        # It's 00:SS.ss format, just return the seconds part
                        return float(second_part)
                    else:
                        # It's MM:SS.ss format, convert normally
                        return float(int(first_part) * 60 + float(second_part))
        
        # Convert time formats with two colons (like XX:XX:XX, XX:XX.XX)
        if count_colon == 2:
            # For 10,000m, 5000m, and 1500m, replace the 6th character with '.' for format XX:XX.XX
            if ('10,000m' in string or '5000m' in string or '1500m' in string):
                if len(metric_str) == 7:  # X:XX:XX (1500m special case)
                    idx = 4
                    metric_mod = '0' + metric_str[:idx] + '.' + metric_str[idx+1:]
                else:
                    idx = 5
                    metric_mod = metric_str[:idx] + '.' + metric_str[idx+1:]
                m, s = metric_mod.split(':')[-2:]
                return float((int(m) * 60) + float(s))
            
            # Standard HH:MM:SS
            h, m, s = metric_str.split(':')
            return float(int(h) * 3600 + int(m) * 60 + float(s))
        
        # Handle datetime.time/datetime.datetime objects
        if isinstance(metric, (datetime.time, datetime.datetime)):
            t = str(metric)
            h, m, s = t.split(':')
            return float(int(h) * 3600 + int(m) * 60 + float(s))
        
        # MM:SS.sss format
        if count_colon == 1 and count_dot >= 1:
            m, s = metric_str.split(':')
            return float(int(m) * 60 + float(s))
        
        # HH.MM.SS (rare) or MM:SS:SS
        if count_colon == 1 and count_dot == 2:
            # Replace first dot with colon
            metric_mod = metric_str.replace('.', ':', 1)
            h, m, s = metric_mod.split(':')
            return float(int(h) * 3600 + int(m) * 60 + float(s))
        
        # HH:MM:SS (no dots)
        if count_colon == 2 and count_dot == 0:
            h, m, s = metric_str.split(':')
            return float(int(h) * 3600 + int(m) * 60 + float(s))
        
        # MM:SS (no dots)
        if count_colon == 1 and count_dot == 0:
            m, s = metric_str.split(':')
            return float(int(m) * 60 + int(s))
            
    except Exception:
        return ''
    
    return output

    
#def seconds_to_mmss(seconds):
#    # Robustly check and convert the input to a float
#    try:
#        seconds = np.float64(seconds)
#    except (ValueError, TypeError):
#        return ''
            
#    # Check for NaN/missing values after conversion
#    if pd.isna(seconds) or seconds < 0:
#        return ''
        
#    # 1. Check if the time is 1 hour (3600 seconds) or longer
#    if seconds >= 3600:
#    # Use HH:MM:SS.ss format for longer events
#    # Standard divmod calculation for hours, minutes, and remaining seconds
#        hours, remainder = divmod(seconds, 3600)
#        minutes, secs = divmod(remainder, 60)
                
#    # Ensure hours and minutes are integers for formatting
#        hours = int(hours)
#        minutes = int(minutes)
                
#    # Return full HH:MM:SS.ss format
#        return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"
            
#    else:
#                # Use MM:SS.ss format for events under 1 hour.
#                # This requires calculating the total minutes (which may be > 59)
                
#                # Total minutes (e.g., 59 for 59:00.00)
#        total_minutes = int(seconds / 60)
#                # Remaining seconds (with decimals)
#        remaining_secs = seconds % 60
                
#                # Return MM:SS.ss format
#        return f"{total_minutes:02d}:{remaining_secs:05.2f}"

def seconds_to_mmss(seconds):
    
    # Robustly check and convert the input to a float
    try:
        seconds = np.float64(seconds)
    except (ValueError, TypeError):
        return ''
            
    # Check for NaN/missing values after conversion
    if pd.isna(seconds) or seconds < 0:
        return ''
        
    # 1. Standard calculation applied universally:
    # Hours will be 0 if the total time is less than 3600 seconds (1 hour)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
                
    # Ensure hours and minutes are integers for formatting
    hours = int(hours)
    minutes = int(minutes)
                
    # 2. Return full HH:MM:SS.ss format consistently
    # :02d ensures two digits (e.g., 00)
    # :05.2f ensures SS.ss format (e.g., 05.23)
    return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"

def map_nwi(df):
    
    wind_series = df['WIND']
    
    # 1. Check for standard null values (NaN or None)
    mask_isna = pd.isna(wind_series)
    
    # 2. Check for empty strings or explicit text placeholders
    # Must convert to string *before* using .str methods
    wind_str = wind_series.astype(str).str.strip().str.lower()
    
    mask_empty_str = (wind_str == '')
    mask_placeholders = wind_str.isin(['nan', 'none', '-'])
    
    # Combine all masks
    mask_wind_is_missing = mask_isna | mask_empty_str | mask_placeholders

    # 4. Apply the mask: Set the 'WIND' field to 'NWI'
    df.loc[mask_wind_is_missing, 'WIND'] = 'NWI'

    return df


                            
                 
