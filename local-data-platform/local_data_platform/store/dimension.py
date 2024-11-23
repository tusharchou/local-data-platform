from . import Store
from typing import List, Dict
from pathlib import Path
from pandas import DataFrame

class Dimension(Store):
    def __init__(self, name: str, data: DataFrame):
        self.name = name
        self.data = data
    def get(self):
        return self.data

    def get_name(self):
        return self.name
