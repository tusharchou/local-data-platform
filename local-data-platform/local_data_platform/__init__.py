from abc import ABC
from enum import Enum
from dataclasses import dataclass, asdict

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
            format: SupportedFormat,
            *args,
            **kwargs
    ):
        self.name = name
        self.path = path
        self.format = format
        self.metadata = kwargs


@dataclass
class BaseConfig(Base):
    __slots__ = ("identifier","database","path")
    identifier: str
    database: str
    path: str

