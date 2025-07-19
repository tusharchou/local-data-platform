from local_data_platform.pipeline.egression import Egression
from pyarrow import parquet, Table
from local_data_platform.format.parquet import Parquet
from local_data_platform.format.iceberg import Iceberg
from local_data_platform import Config
from local_data_platform.logger import log


logger = log()


class ParquetToIceberg(Egression):
    """
    ParquetToIceberg is a class responsible for transforming data from a Parquet source to an Iceberg target.

    Attributes:
        source (Parquet): The source Parquet configuration.
        target (Iceberg): The target Iceberg configuration.

    Methods:
        __init__(config: Config, *args, **kwargs):
            Initializes the ParquetToIceberg instance with the provided configuration.
            Args:
                config (Config): Configuration object containing metadata for source and target.
                *args: Variable length argument list.
                **kwargs: Arbitrary keyword arguments.
    """
    def __init__(self, config: Config, *args, **kwargs):
        self.source = config.metadata['source']
        self.target = config.metadata['target']
        self.source = Parquet(
            name=self.source['name'],
            path=self.source['path']
        )
        self.target = Iceberg(
            name=self.target['name'],
            config=self.target['catalog']
        )
        logger.info(
            f"""
            ParquetToIceberg initialised with
            source {self.source}
            target {self.target}
            """
        )
