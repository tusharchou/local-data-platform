import os
from local_data_platform.catalog.sql import LocalCatalog
from local_data_platform.source.parquet.pyarrow_table import PyarrowTable

config = {
    "name": "NYC Yellow Taxi",
    "path": "/Users/tushar/Documents/GitHub/local-data-platform/local-data-platform/yellow_tripdata_2023-01.parquet",
    "warehouse_path": "./tmp/warehouse"
}

warehouse_path = config['warehouse_path']
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

'''
CLI COMMAND to grab data -
    curl https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet -o /tmp/yellow_tripdata_2023-01.parquet
References:
    https://py.iceberg.apache.org/#write-a-pyarrow-dataframe
'''
path = config['path']
nyc_yellow_taxi_rides = PyarrowTable()
df = nyc_yellow_taxi_rides.from_parquet(path)

# catalog.create_namespace("pyiceberg_catalog_db")

# table = catalog.create_table(
#     "pyiceberg_catalog_db.taxi_dataset",
#     schema=df.schema,
# )

nyc_yellow_taxi_rides.append(df)
len(table.scan().to_arrow())
