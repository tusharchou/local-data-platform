"""
local_data_platform
==================

This module provides core classes and enums for building a local data platform,
including support for data formats, engines, tables, pipelines, and configuration.
"""

from abc import ABC
from enum import Enum
from pathlib import Path
import os
from dataclasses import dataclass, asdict
from .exceptions import TableNotFound, PipelineNotFound, EngineNotFound
from collections import namedtuple

Transaction = namedtuple("Transaction", ["query", "desc"])

__all__ = [
    "Transaction",
    "SupportedFormat",
    "SupportedEngine",
    "Base",
    "Table",
    "Flow",
    "Worker",
    "Config",
    "Credentials",
]


class SupportedFormat(Enum):
    """
    Enum for supported data formats.
    """

    ICEBERG = "ICEBERG"
    PARQUET = "PARQUET"
    CSV = "CSV"
    JSON = "JSON"


class SupportedEngine(Enum):
    """
    Enum for supported data processing engines.
    """

    PYARROW = "PYARROW"
    PYSPARK = "PYSPARK"
    DUCKDB = "DUCKDB"
    BIGQUERY = "BIGQUERY"


class Base(ABC):
    """
    Abstract base class for all platform components.
    """

    def __init__(self, *args, **kwargs):
        pass

    def get(self):
        """Retrieve data or resource."""
        pass

    def put(self):
        """Store data or resource."""
        pass


class Table(Base):
    """
    Represents a data table in the platform.
    """

    def __init__(self, name: str, path: Path = os.getcwd()):
        self.name = name
        self.path = os.getcwd() + path

    def get(self):
        """Raise TableNotFound if table cannot be accessed."""
        raise TableNotFound(
            f"Table {self.name} of type {self.format} cannot be accessed at {self.path}"
        )

    def put(self):
        """Raise TableNotFound if table cannot be accessed."""
        raise TableNotFound(
            f"Table {self.name} of type {self.format} cannot be accessed at {self.path}"
        )


class Flow(Base):
    """
    Represents a data pipeline (ETL flow) in the platform.
    """

    name: str
    source: Table
    target: Table

    def extract(self):
        """Raise PipelineNotFound if extraction fails."""
        raise PipelineNotFound(
            f"Pipeline {self.name} cannot extract data from {self.source.name}"
        )

    def transform(self):
        """Raise PipelineNotFound if transformation fails."""
        raise PipelineNotFound(
            f"Pipeline {self.name} cannot transform data from {self.source.name}"
        )

    def load(self):
        """Raise PipelineNotFound if loading fails."""
        raise PipelineNotFound(
            f"Pipeline {self.name} cannot load data at {self.target.name}"
        )


class Worker(Base):
    """
    Represents a compute engine or worker in the platform.
    """

    def __init__(self, name: str):
        self.name = name

    def get(self):
        """Raise EngineNotFound if engine is not supported."""
        raise EngineNotFound(f"Worker {self.name} is not a supported engine")

    def put(self):
        """Raise EngineNotFound if engine is not supported."""
        raise EngineNotFound(f"Worker {self.name} is not a supported engine")


@dataclass
class Config(Base):
    """
    Configuration for a data pipeline or resource.
    """

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
    """
    Credentials for accessing cloud or local resources.
    """

    __slots__ = ("path", "project_id")
    path: Path
    project_id: str
