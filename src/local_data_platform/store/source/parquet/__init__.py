from local_data_platform.store.source import Source
from pyarrow import parquet, Table


class Parquet(Source):
    """
    A base class for Parquet File implementation
    """


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get(self) -> Table:
        return parquet.read_table(self.path)

