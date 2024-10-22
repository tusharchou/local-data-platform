from pyiceberg.catalog.sql import SqlCatalog
from typing import List
from pyiceberg.typedef import Identifier
from pyiceberg.table import Table


class LocalIcebergCatalog(SqlCatalog):

    def __init__(self, name: str, path: str, *args, **kwargs):
        self.name = name
        self.uri = f"sqlite:///{path}/pyiceberg_catalog.db"  # Ensure .db file extension
        self.warehouse = f"file://{path}"
        super().__init__(*args, **kwargs, **self.__dict__)

    def get_dbs(self) -> List[Identifier]:
        return self.list_namespaces()

    def get_tables(self, namespace: Identifier) -> List[Identifier]:
        return self.list_tables(namespace=namespace)

    def get_table(self, name: Identifier) -> Table:
        return self.load_table(identifier=name)

