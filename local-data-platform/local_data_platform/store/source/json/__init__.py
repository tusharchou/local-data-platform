import json
from local_data_platform.store.source import Source

class Json(Source):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get(self):
        with open(self.path, 'r') as file:
            return json.load(file)