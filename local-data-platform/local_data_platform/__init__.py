from abc import ABC
from enum import Enum

class SupportedFormat(Enum):
    ICEBERG = 1
    PARQUET = 2
    CSV = 3




class Base(ABC):

    def get(self):
        pass

    def put(self):
        pass


class Table(Base):

    def __init__(
            self,
            name: str,
            path: str,
            format: SupportedFormat,
            metadata: dict,
    ):
        self.name = name
        self.path = path
        self.format = format
        self.metadata = metadata

