from pyiceberg.catalog.sql import SqlCatalog

class LocalIcebergCatalog(SqlCatalog):

    def __init__(self,  name: str, **properties: str):
        # Set up the SQL catalog using SQLite and the defined warehouse path
        super().__init__(name, **properties)



