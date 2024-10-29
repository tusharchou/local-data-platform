from local_data_platform.format import Format
from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
from pyiceberg.schema import Schema
from pyiceberg.typedef import Identifier
from pyarrow import Table
from local_data_platform.logger import log

logger = log()


class Iceberg(Format):

    def __init__(self, catalog: str, *args, **kwargs):
        self.catalog = LocalIcebergCatalog(
            catalog['identifier'],
            path=catalog['warehouse_path']
        )

        self.identifier = f"{catalog['identifier']}.{kwargs['name']}"
        self.metadata = kwargs
        super().__init__(*args, **kwargs)

    def put(self, df: Table) -> Table:
        logger.info(f"self.identifier {self.identifier}")
        logger.info(
            f"""
            Writing {len(df)} to Iceberg Table 
            """
        )
        table = self.catalog.create_table_if_not_exists(identifier=self.identifier, schema=df.schema)
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
            Returning {len(data)} records from Iceberg Table
            """
        )
        return data
