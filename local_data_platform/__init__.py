from abc import ABC
from enum import Enum
from pathlib import Path
import os
from dataclasses import dataclass, asdict
from .exceptions import TableNotFound, PipelineNotFound, EngineNotFound
from collections import namedtuple

Transaction = namedtuple("Transaction", ["query", "desc"])


class SupportedFormat(Enum):
    ICEBERG = "ICEBERG"
    PARQUET = "PARQUET"
    CSV = "CSV"
    JSON = "JSON"


class SupportedEngine(Enum):
    PYARROW = "PYARROW"
    PYSPARK = "PYSPARK"
    DUCKDB = "DUCKDB"
    BIGQUERY = "BIGQUERY"


class Base(ABC):

    def __init__(self, *args, **kwargs):
        pass

    def get(self):
        pass

    def put(self):
        pass


class Table(Base):

    def __init__(self, name: str, path: Path = os.getcwd()):
        self.name = name
        self.path = os.getcwd()+path

    def get(self):
        raise TableNotFound(
            f"Table {self.name} of type {self.format} cannot be accessed at {self.path}"
        )

    def put(self):
        raise TableNotFound(
            f"Table {self.name} of type {self.format} cannot be accessed at {self.path}"
        )


class Flow(Base):
    name: str
    source: Table
    target: Table

    def extract(self):
        raise PipelineNotFound(
            f"Pipeline {self.name} cannot extract data from {self.source.name}"
        )

    def transform(self):
        raise PipelineNotFound(
            f"Pipeline {self.name} cannot transform data from {self.source.name}"
        )

    def load(self):
        raise PipelineNotFound(
            f"Pipeline {self.name} cannot load data at {self.target.name}"
        )


class Worker(Base):

    def __init__(self, name: str):
        self.name = name

    def get(self):
        raise EngineNotFound(f"Worker {self.name} is not a supported engine")

    def put(self):
        raise EngineNotFound(f"Worker {self.name} is not a supported engine")


@dataclass
class Config(Base):
    __slots__ = ("identifier", "who", "metadata")
    identifier: str
    who: str
    what: str
    where: str
    when: str
    how: str
    metadata: Flow


@dataclass
class Credentials(Base):
    __slots__ = ("path", "project_id")
    path: Path
    project_id: str
