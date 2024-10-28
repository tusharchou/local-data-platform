from local_data_platform.store.source.gcp.bigquery import BigQuery  
from pathlib import Path
import os
from local_data_platform import Config,Transaction
from local_data_platform.store.source.json import Json



"""
creadentials.json is the path to the credentials file that you downloaded from GCP.
"""  
local_gcp_credential_path = Path(os.getcwd()+'/local-data-platform/samples/creadentials.json')
dataset = 'near_transactions'
config_path = Path(os.getcwd()+'/local-data-platform/real_world_use_cases/near_data_lake/config/ingestion.json')
config = Config(
            **Json(
            name=dataset,
            path=config_path,
        ).get()
)

store = BigQuery(credential_path=local_gcp_credential_path)
df = store.query(NEAR_TRANSACTION_QUERY.query)

