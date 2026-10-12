"""local-data-platform: a Python API layer for a lakehouse that runs on your laptop.

Importing the package has no side effects: it configures no logging, sets no
environment variables and needs no optional dependencies.
"""

import os
from abc import ABC
from collections import namedtuple
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import Config
from .exceptions import EngineNotFound, PipelineNotFound, TableNotFound
from .paths import resolve_path

__version__ = "0.1.1"

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
    """Root of the class hierarchy: anything that can ``get`` or ``put`` data."""

    def __init__(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        raise NotImplementedError(f"{type(self).__name__}.get() is not implemented")

    def put(self, *args, **kwargs):
        raise NotImplementedError(f"{type(self).__name__}.put() is not implemented")


class Table(Base):
    """A named dataset at a path, in a format.

    ``path`` follows :func:`local_data_platform.paths.resolve_path`: absolute paths
    are kept, relative paths resolve against ``base_dir`` (or the cwd).
    """

    def __init__(self, name: str, path: str | os.PathLike | None = None, format: str | None = None,
                 base_dir: str | os.PathLike | None = None, **options):
        self.name = name
        self.path = resolve_path(path, base_dir) if path else None
        self.format = (format or type(self).__name__).upper()
        self.options = options

    def get(self, *args, **kwargs):
        raise TableNotFound(f"Table {self.name} of type {self.format} cannot be read at {self.path}")

    def put(self, *args, **kwargs):
        raise TableNotFound(f"Table {self.name} of type {self.format} cannot be written at {self.path}")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r}, path={str(self.path) if self.path else None!r})"


class Flow(Base):
    """Extract, transform and load. Concrete pipelines live in ``local_data_platform.pipeline``."""

    name: str = "flow"
    source: Table
    target: Table

    def _describe(self, attr: str) -> str:
        obj = getattr(self, attr, None)
        return getattr(obj, "name", repr(obj))

    def extract(self):
        raise PipelineNotFound(f"Pipeline {self.name} cannot extract data from {self._describe('source')}")

    def transform(self, *args, **kwargs):
        raise PipelineNotFound(f"Pipeline {self.name} cannot transform data from {self._describe('source')}")

    def load(self):
        raise PipelineNotFound(f"Pipeline {self.name} cannot load data at {self._describe('target')}")


class Worker(Base):

    def __init__(self, name: str):
        self.name = name

    def get(self, *args, **kwargs):
        raise EngineNotFound(f"Worker {self.name} is not a supported engine")

    def put(self, *args, **kwargs):
        raise EngineNotFound(f"Worker {self.name} is not a supported engine")


@dataclass
class Credentials:
    path: Path
    project_id: str | None = None


__all__ = [
    "__version__",
    "Base",
    "Config",
    "Credentials",
    "Flow",
    "SupportedEngine",
    "SupportedFormat",
    "Table",
    "Transaction",
    "Worker",
    "resolve_path",
]
