import os
from local_data_platform.catalog.sql import LocalCatalog

config = {
    "name": "NYC Yellow Taxi",
    "path": "/Users/tushar/Documents/GitHub/local-data-platform/sample_data/parquet/nyc_yellow_taxi/2023-01/yellow_tripdata_2023-01.parquet"
}
# Define the warehouse path
warehouse_path = "./tmp/warehouse"

# Ensure the directory exists
os.makedirs(warehouse_path, exist_ok=True)


catalog = LocalCatalog(
    "pyiceberg_catalog_db",
    **{
        "uri": f"sqlite:///{warehouse_path}/pyiceberg_catalog.db",  # Ensure .db file extension
        "warehouse": f"file://{warehouse_path}",
    },
)


# Verify if the catalog is set up correctly
print("Catalog set up successfully:", catalog)
