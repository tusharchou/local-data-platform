from local_data_platform.store.source.gcp.bigquery import BigQuery  
from local_data_platform.real_world_use_cases.near_data_lake.config.sample_queries.near_transactions import NEAR_TRANSACTION_QUERY
from pathlib import Path
import os

"""
creadentials.json is the path to the credentials file that you downloaded from GCP.
"""  
local_gcp_credential_path =   Path(os.getcwd()+'/local-data-platform/samples/credentials.json')
store = BigQuery(credential_path=local_gcp_credential_path)

df = store.query(NEAR_TRANSACTION_QUERY.query)

