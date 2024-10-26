import pandas as pd
import json
import os
from pyiceberg.catalog import load_catalog

def flatten_and_load_to_iceberg(input_file: str, catalog_name: str, table_name: str) -> None:
    """Flatten raw JSON data and load it into an Iceberg Bronze table."""
    # Load and flatten JSON data
    with open(input_file, 'r') as f:
        raw_data = json.load(f)
    flat_df = pd.json_normalize(raw_data)

    # Load the Iceberg catalog
    catalog = load_catalog(catalog_name)

    # Write flattened data to Iceberg Bronze table
    table = catalog.load_table(table_name)
    table.write_pandas(flat_df)

    print(f"Bronze layer data loaded into Iceberg table: {table_name}")

if __name__ == "__main__":
    input_path = "data/raw/airbnb_data.json"
    flatten_and_load_to_iceberg(
        input_file=input_path,
        catalog_name="my_iceberg_catalog",
        table_name="bronze.airbnb_flat"
    )
