from . import PyArrowLoader
from pyarrow import parquet, Table
from local_data_platform.format.parquet import Parquet
from local_data_platform.format.iceberg import Iceberg
from local_data_platform import Config
from local_data_platform.logger import log


logger = log()


class ParquetToIceberg(PyArrowLoader):

    def __init__(self, config: Config, *args, **kwargs):
        self.source = config.metadata['source']
        self.target = config.metadata['target']
        self.source = Parquet(
            name=self.source['name'],
            path=self.source['path']
        )
        self.target = Iceberg(
            name=self.target['name'],
            catalog=self.target['catalog']
        )
        logger.info(
            f"""
            ParquetToIceberg initialised with
            source {self.source}
            target {self.target}
            """
        )
