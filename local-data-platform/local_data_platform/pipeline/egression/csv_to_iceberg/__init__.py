from local_data_platform.pipeline.egression import Egression
from pyarrow import parquet, Table
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg
from local_data_platform import Config
from local_data_platform.logger import log


logger = log()


class CsvToIceberg(Egression):

    def __init__(self, config: Config, *args, **kwargs):
        logger.info(
            f"""
            Initialising CsvToIceberg with config {config}
            """
        )
        self.source = config.metadata['source']
        self.target = config.metadata['target']
        self.source = CSV(
            name=self.source['name'],
            path=self.source['path']
        )
        self.target = Iceberg(
            name=self.target['name'],
            catalog=self.target['catalog']
        )
        logger.info(
            f"""
            CsvToIceberg initialised with
            source {self.source}
            target {self.target}
            """
        )
