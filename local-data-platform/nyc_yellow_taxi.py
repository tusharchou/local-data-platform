import os

from local_data_platform.catalog.sql import LocalIcebergCatalog
from local_data_platform.target.iceberg.table import IcebergTable
from local_data_platform.source.parquet.pyarrow_table import PyarrowTable
from local_data_platform import BaseConfig


config = {
    "name": "nyc_yellow_taxi_rides",
    "path": "/Users/tushar/Documents/GitHub/local-data-platform/local-data-platform/yellow_tripdata_2023-01.parquet",
    "warehouse_path": "./tmp/warehouse",
    "catalog_identifier": "pyiceberg_catalog_db",
    "format": "PARQUET"
}

class NYCYellowTaxiRides(IcebergTable):

    def __init__(self, config: BaseConfig, *args, **kwargs):
        self.warehouse_path = config['warehouse_path']
        self.catalog_identifier = config['catalog_identifier']
        # self.catalog_uri = config['catalog_uri']
        try:
            # Ensure the directory exists
            os.makedirs(self.warehouse_path, exist_ok=True)
        except:
            pass
        try:
            self.catalog = LocalIcebergCatalog(
                self.catalog_identifier,
                **{
                    "uri": f"sqlite:///{self.warehouse_path}/pyiceberg_catalog.db",  # Ensure .db file extension
                    "warehouse": f"file://{self.warehouse_path}",
                },
            )
        except:
            pass
        print(f"arg {args}")
        print(f"config {config}")
        super(NYCYellowTaxiRides, self).__init__(catalog=self.catalog, **config)



table = NYCYellowTaxiRides(config)

# Verify if the catalog is set up correctly
print("Catalog set up successfully:", table.catalog)

'''
CLI COMMAND to grab data -
    curl https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet -o /tmp/yellow_tripdata_2023-01.parquet
References:
    https://py.iceberg.apache.org/#write-a-pyarrow-dataframe
'''
path = config['path']
nyc_yellow_taxi_rides = PyarrowTable()
df = nyc_yellow_taxi_rides.from_parquet(path)
try:
    table.catalog.create_namespace("pyiceberg_catalog_db")
except:
    pass


table = NYCYellowTaxiRides(config)

print(f"""
    Config {config}
    df.schema {df.schema}
    table.name {table.name}
    table.catalog_identifier {table.catalog_identifier}
""")

local_table = table.put(schema=df.schema)

local_table.append(df)
print(len(local_table.scan().to_arrow()))
