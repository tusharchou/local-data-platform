from local_data_platform.store.source import Source
from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
from pyiceberg.schema import Schema
from pyiceberg.typedef import Identifier
from pyiceberg.table import Table

class Iceberg(Source):

    def __init__(self, catalog: str, *args, **kwargs):
        self.catalog = LocalIcebergCatalog(
            catalog['identifier'],
            path=catalog['warehouse_path']
        )

        self.identifier = f"{catalog['identifier']}.{kwargs['name']}"
        self.path = kwargs["path"]
        self.format = kwargs["format"]
        self.metadata = kwargs
        super().__init__(*args, **kwargs)

    # def put(self, schema: Schema) -> Table:
    #     print(f"self.identifier {self.identifier}")
    #     return self.catalog.create_table_if_not_exists(identifier=self.identifier, schema=schema)

    def get_schema(self) -> Schema:
        return self.catalog.load_table()

    def get_10_rows(self, catalog, name):
        pass
