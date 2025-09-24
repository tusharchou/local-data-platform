from local_data_platform.format import Format
from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
from pyiceberg.schema import Schema
from pyiceberg.typedef import Identifier
from pyarrow import Table
from local_data_platform.logger import log
import os

os.environ['PYICEBERG_DOWNCAST_NS_TIMESTAMP_TO_US_ON_WRITE'] = 'true'


logger = log()


class Iceberg(Format):
    """
    Iceberg class for handling data operations with Iceberg tables.

    Attributes:
        catalog_identifier (str): Identifier for the Iceberg catalog.
        catalog (LocalIcebergCatalog): Instance of LocalIcebergCatalog for managing Iceberg tables.
        identifier (str): Unique identifier for the Iceberg table.
        metadata (dict): Metadata associated with the Iceberg table.

    Methods:
        __init__(catalog: str, *args, **kwargs):
            Initializes the Iceberg instance with the given catalog and metadata.

        put(df: Table) -> Table:
            Writes the given data frame to the Iceberg table.

        get():
            Fetches data from the Iceberg table and returns it as an Arrow table.
    """
    def __init__(self, config: str, *args, **kwargs):
        logger.info(f"Iceberg catalog : {config}")
        self.catalog_identifier = config["identifier"]
        self.catalog = LocalIcebergCatalog(
            self.catalog_identifier, path=config["warehouse_path"]
        )
        if not self.catalog._namespace_exists(self.catalog_identifier):
            self.catalog.create_namespace(self.catalog_identifier)
        self.identifier = f"{self.catalog_identifier}.{kwargs['name']}"
        self.metadata = kwargs
        logger.info(f"Iceberg created with catalog namespace {self.catalog_identifier}")
        logger.info(f"Iceberg initialised with identifier {self.identifier}")
        super().__init__(*args, **kwargs)

    def put(self, df: Table) -> Table:
        if not df:
            logger.error(f"While doing put in Iceberg Format we got df as None")
            raise Exception(f" Got Table as non")
        logger.info(f"self.identifier {self.identifier}")
        logger.info(
            f"""
            Writing type {type(df)} of length {len(df)} to Iceberg Table {self.identifier}
            """
        )
        table = self.catalog.create_table_if_not_exists(
            identifier=self.identifier, schema=df.schema, properties={
                "downcast-ns-timestamp-to-us-on-write": True  # Set property for downcasting
            }
        )
        table.append(df)
        return table

    def get(self):
        logger.info(
            f"""
            Fetching data from Iceberg Table
            """
        )
        data = self.catalog.load_table(self.identifier).scan().to_arrow()
        logger.info(
            f"""
            Returning {len(data)} records from Iceberg Table {self.identifier}
            """
        )
        return data
