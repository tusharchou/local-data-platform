from local_data_platform import Table
from local_data_platform.catalog.sql import LocalIcebergCatalog
from pyiceberg.schema import Schema
from pyiceberg.typedef import Identifier


class IcebergTable(Table):

    def __init__(self, catalog: LocalIcebergCatalog, *args, **kwargs):
        print(f" kwargs {kwargs}")
        self.catalog = catalog
        self.name = f"{kwargs['catalog_identifier']}.{kwargs['name']}"
        print(f"icebergs table name id  {self.name}")
        self.path = kwargs["path"]
        self.format = kwargs["format"]
        self.metadata = kwargs
        super(IcebergTable, self).__init__(*args, **kwargs)

    def put(self, schema: Schema) -> Table:
        print(f"self.name {self.name}")
        return self.catalog.create_table_if_not_exists(identifier=self.name, schema=schema)

    def get_10_rows(self, catalog, name):
        pass
