from pyiceberg.catalog.sql import SqlCatalog
from typing import List
from pyiceberg.typedef import Identifier
from sqlite3 import OperationalError
from local_data_platform.logger import log

logger = log()
class LocalIcebergCatalog(SqlCatalog):
    """
    LocalIcebergCatalog is a subclass of SqlCatalog that provides methods to interact with a local Iceberg catalog using SQLite.
    Attributes:
        name (str): The name of the catalog.
        uri (str): The URI for the SQLite database.
        warehouse (str): The file path for the warehouse.
    Methods:
        __init__(name: str, path: str, *args, **kwargs):
            Initializes the LocalIcebergCatalog with the given name and path.
            Args:
                name (str): The name of the catalog.
                path (str): The file path where the catalog is stored.
                *args: Variable length argument list.
                **kwargs: Arbitrary keyword arguments.
            Raises:
                Exception: If initialization fails.
        get_dbs() -> List[Identifier]:
            Returns a list of database identifiers in the catalog.
        get_tables(namespace: Identifier):
            Returns a list of tables in the specified namespace.
    """

    def __init__(self, name: str, path: str, *args, **kwargs):
        self.name = name
        self.uri = f"sqlite:///{path}/{name}_catalog.db"
        self.warehouse = f"file://{path}"
        try: 
            logger.info(f"Initializing LocalIcebergCatalog with {self.uri}")
            super().__init__(*args, **kwargs, **self.__dict__)
        except Exception as e:
            logger.error(f"Failed to initialize LocalIcebergCatalog {e}")
            raise Exception(f"Failed to initialize LocalIcebergCatalog {e}") 
        
    def get_dbs(self) -> List[Identifier]:
        return self.list_namespaces()

    def get_tables(self, namespace: Identifier):
        return self.list_tables(namespace=namespace)



