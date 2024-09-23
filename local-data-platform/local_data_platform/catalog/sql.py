import os
from pyiceberg.catalog.sql import SqlCatalog

class LocalCatalog(SqlCatalog):

    def __init__(self,  name: str, **properties: str):
        super().__init__(name, **properties)



# Define the warehouse path
warehouse_path = "./tmp/warehouse"

# Ensure the directory exists
os.makedirs(warehouse_path, exist_ok=True)

# Set up the SQL catalog using SQLite and the defined warehouse path
catalog = SqlCatalog(
    "pyiceberg_catalog_db",
    **{
        "uri": f"sqlite:///{warehouse_path}/pyiceberg_catalog.db",  # Ensure .db file extension
        "warehouse": f"file://{warehouse_path}",
    },
)

# Verify if the catalog is set up correctly
print("Catalog set up successfully:", catalog)
