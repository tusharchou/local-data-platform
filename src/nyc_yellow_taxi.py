import os
from pathlib import Path

from local_data_platform.catalog.sql import LocalIcebergCatalog
from local_data_platform import BaseConfig
from local_data_platform.source.parquet.pyarrow_table import PyarrowTable
from local_data_platform.target.iceberg.table import IcebergTable


class NYCYellowTaxiRides(IcebergTable):
    """An Iceberg table representing NYC Yellow Taxi ride data."""

    def __init__(self, config: BaseConfig, *args, **kwargs):
        self.warehouse_path = config["warehouse_path"]
        self.catalog_identifier = config["catalog_identifier"]
        # Ensure the directory exists
        os.makedirs(self.warehouse_path, exist_ok=True)
        self.catalog = LocalIcebergCatalog(
            self.catalog_identifier,
            **{
                "uri": f"sqlite:///{self.warehouse_path}/pyiceberg_catalog.db",
                "warehouse": f"file://{self.warehouse_path}",
            },
        )
        super(NYCYellowTaxiRides, self).__init__(catalog=self.catalog, **config)


def main():
    """Main function to demonstrate creating and appending to an Iceberg table."""
    project_root = Path(__file__).parent.parent
    data_file = project_root / "data" / "yellow_tripdata_2023-01.parquet"
    warehouse_path = project_root / "tmp" / "warehouse"

    config = {
        "name": "nyc_yellow_taxi_rides",
        "path": str(data_file),
        "warehouse_path": str(warehouse_path),
        "catalog_identifier": "pyiceberg_catalog_db",
        "format": "PARQUET",
    }

    table = NYCYellowTaxiRides(config)
    print("Catalog set up successfully:", table.catalog)
    
    path = config["path"]
    if not os.path.exists(path):
        print(f"Data file not found at {path}")
        print(
            "Please download it using: curl https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet -o <path>"
        )
        return

    nyc_yellow_taxi_rides = PyarrowTable()
    df = nyc_yellow_taxi_rides.from_parquet(path)

    local_table = table.put(schema=df.schema)
    local_table.append(df)
    print(f"Table contains {len(local_table.scan().to_arrow())} rows.")


# This ensures the script only runs when executed directly
if __name__ == "__main__":
    main()
