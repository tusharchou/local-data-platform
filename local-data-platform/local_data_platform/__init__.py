from abc import ABC
from enum import Enum
from dataclasses import dataclass, asdict
from .exceptions import TableNotFound

class SupportedFormat(Enum):
    ICEBERG = 1
    PARQUET = 2
    CSV = 3


class Base(ABC):

    def __init__(self, *args, **kwargs): pass

    def get(self):
        pass

    def put(self):
        pass


class Table(Base):

    def __init__(
            self,
            name: str,
            path: str,
            format: SupportedFormat
    ):
        self.name = name
        self.path = path
        self.format = format

    def get(self):
        raise TableNotFound(f"Table {self.name} of type {self.format} cannot be accessed at {self.path}")

    def put(self):
        raise TableNotFound(f"Table {self.name} of type {self.format} cannot be accessed at {self.path}")

@dataclass
class Config(Base):
    __slots__ = ("identifier", "who", "metadata")
    identifier: str
    who: str
    what: str
    where: str
    when: str
    how: str
    metadata: str




class Flow(Base):
    source: Table
    target: Table

    def extract(self):
        pass

    def transform(self):
        pass

    def load(self):
        pass



