from local_data_platform.pipeline.ingestion import Ingestion
from pyarrow import parquet, Table
from local_data_platform.store.source.parquet import Parquet
from local_data_platform.store.target.iceberg import Iceberg
from local_data_platform import Config

class PyArrow(Ingestion):

    def __init__(self, config: Config, *args, **kwargs):
        self.config = config
        self.source = Parquet(**config.metadata['source'])
        self.target = Iceberg(**config.metadata['target'])

    def extract(self) -> Table:
        self.df = parquet.read_table(self.source.path)


    def load(self):
        self.target.put(self.df.schema)
