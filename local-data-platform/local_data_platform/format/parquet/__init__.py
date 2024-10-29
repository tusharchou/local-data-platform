from local_data_platform.format import Format
from pyarrow import parquet, Table
from local_data_platform.logger import log
import os


logger = log()


class Parquet(Format):
    """
    A base class for Parquet File implementation
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get(self) -> Table:
        logger.info(
            f"""
            reading parquet from {self.path}
            """
        )
        if not os.path.isfile(self.path):
            raise FileNotFoundError

        df = parquet.read_table(self.path)
        logger.info(
            f"""
            df type {type(df)}
            df records {len(df)}
            """
        )
        if df is not None:
            return df
        else:
            raise FileNotFoundError(f"Parquet file not found on path {self.path}")
