from local_data_platform.format import Format
from pyarrow import csv, Table
from local_data_platform.logger import log
import os


logger = log()


class CSV(Format):
    """
    A base class for CSV File implementation
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get(self) -> Table:
        if not os.path.isfile(self.path):
            raise FileNotFoundError

        logger.info(
            f"""
            reading CSV from {self.path}
            """
        )
        df = csv.read_table(self.path)
        logger.info(
            f"""
            df type {type(df)}
            """
        )
        if df is not None:
            return df
        else:
            raise FileNotFoundError(f"CSV file not found on path {self.path}")

    def put(self, df: Table):
        logger.info(
            f"""
            Writing data from PyArrow Table of size {len(df)} records
            """
        )
        with open(self.path, 'wb') as f:
            csv.write_csv(df, f)
