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
            logger.error(f"This path {self.path} is invalid")
            raise FileNotFoundError

        logger.info(
            f"""
            reading CSV from {self.path}
            """
        )
        df = csv.read_csv(self.path)
        logger.info(
            f"""
            df type {type(df)} len {len(df)}
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
        if df is not None or len(df) > 0:
            with open(self.path, "wb") as f:
                csv.write_csv(df, f)
        else:
            logger.error("No data to write to CSV as the data is empty")
            raise ValueError("No data to write")
