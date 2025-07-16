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
        
        logger.info(f"Reading CSV from {self.path}")
        try:
            df = csv.read_csv(self.path)
        except Exception as e:
            logger.error(f"Failed to read or parse CSV file at {self.path}: {e}")
            raise IOError(f"Could not read CSV file at {self.path}") from e

        logger.info(f"Successfully read {len(df)} records from {self.path}")
        return df

    def put(self, df: Table):
        if not df or len(df) == 0:
            logger.error("No data to write to CSV as the DataFrame is empty or None.")
            raise ValueError("Cannot write an empty or None DataFrame to CSV.")

        logger.info(f"Writing {len(df)} records to CSV at {self.path}")
        with open(self.path, "wb") as f:
            csv.write_csv(df, f)
