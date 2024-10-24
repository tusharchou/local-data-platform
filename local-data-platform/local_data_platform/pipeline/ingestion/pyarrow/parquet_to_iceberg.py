from local_data_platform.pipeline.ingestion import Ingestion
from pyarrow import parquet, Table
from local_data_platform.format.parquet import Parquet
from local_data_platform.format.iceberg import Iceberg
from local_data_platform import Config

class ParquetToIceberg(Ingestion):

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
        self._extract()
    def _extract(self) -> Table:
        self.df = parquet.read_table(self.source.path)


    def load(self):
        self.target.put(self.df)
