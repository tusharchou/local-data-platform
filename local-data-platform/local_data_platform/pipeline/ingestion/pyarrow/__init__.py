from local_data_platform.pipeline.ingestion import Ingestion
from pyarrow import Table
from local_data_platform import Config
from local_data_platform.logger import log
import os


logger = log()


class PyArrowLoader(Ingestion):
    """
    PyArrowLoader is a class responsible for loading data using the PyArrow library.

    Attributes:
        config (Config): Configuration object containing settings for the loader.

    Methods:
        __init__(config: Config, *args, **kwargs):
            Initializes the PyArrowLoader with the given configuration.

        _extract() -> Table:
            Extracts data from the source.

        load():
            Loads the extracted data into the target.
    """
    
    def __init__(self, config: Config, *args, **kwargs):
        self.config = config

    def _extract(self) -> Table:
        return self.source.get()

    def load(self):

        self.target.put(self._extract())
