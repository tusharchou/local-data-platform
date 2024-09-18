from abc import ABC

class Pipelines(ABC):
    source: str
    target: str

    def __init__(self):
        pass

    def extract(self):
        pass

    def transform(self):
        pass

    def load(self):
        pass