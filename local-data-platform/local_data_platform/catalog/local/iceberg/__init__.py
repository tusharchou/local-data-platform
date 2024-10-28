from pyiceberg.catalog.sql import SqlCatalog
from typing import List
from pyiceberg.typedef import Identifier
from sqlite3 import OperationalError
from local_data_platform.logger import log

logger = log()
class LocalIcebergCatalog(SqlCatalog):

    def __init__(self, name: str, path: str, *args, **kwargs):
        self.name = name
        self.uri = f"sqlite:///{path}/{name}.db"  # Ensure .db file extension
        self.warehouse = f"file://{path}"
        try: 
            logger.error(f"Initializing LocalIcebergCatalog with {self.uri}")
            super().__init__(*args, **kwargs, **self.__dict__)
        except Exception as e:
            logger.error(f"Failed to initialize LocalIcebergCatalog {e}")
            raise Exception(f"Failed to initialize LocalIcebergCatalog {e}") 
        
    def get_dbs(self) -> List[Identifier]:
        return self.list_namespaces()

    def get_tables(self, namespace: Identifier):
        return self.list_tables(namespace=namespace)



