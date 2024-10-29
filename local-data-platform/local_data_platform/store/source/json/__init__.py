import json
from local_data_platform.store.source import Source
from local_data_platform.logger import log

logger = log()


class Json(Source):

    def __init__(self, *args, **kwargs):
        logger.info(f"Json initialised with {kwargs}")
        super().__init__(*args, **kwargs)

    def get(self):
        logger.info(f"Reading Json with path {self.path}")
        with open(self.path, "r") as file:
            return json.load(file)
