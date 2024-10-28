from local_data_platform.pipeline.ingestion import Ingestion
from pyarrow import Table
from local_data_platform import Config
from local_data_platform.logger import log
import os


logger = log()


class PyArrowLoader(Ingestion):

    def __init__(self, config: Config, *args, **kwargs):
        self.config = config

    def _extract(self) -> Table:
        return self.source.get()

    def load(self):

        self.target.put(self._extract())
