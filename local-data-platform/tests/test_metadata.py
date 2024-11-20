from typing import Optional, List, Union
from local_data_platform.format.iceberg.metadata import (
    table_statistics_from_iceberg_metadata,
    IcebergTableStatistics
)
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.schema import Schema
from pyiceberg.catalog import Catalog
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.table import Table
from pyiceberg.typedef import Properties
import pyarrow as pa
import pytest
from pyiceberg.catalog.sql import SqlCatalog

warehouse_path = "sample_data/"
catalog = SqlCatalog(
    "default",
    **{
        "uri": f"sqlite:///{warehouse_path}/pyiceberg_catalog.db",
        "warehouse": f"file://{warehouse_path}",
    },
)

UNPARTITIONED_PARTITION_SPEC = PartitionSpec(spec_id=0)

def _create_table(
    session_catalog: Catalog,
    identifier: str,
    properties: Properties = {},
    data: Optional[List[pa.Table]] = None,
    partition_spec: PartitionSpec = UNPARTITIONED_PARTITION_SPEC,
) -> Table:

    try:
        session_catalog.drop_table(identifier=identifier)
    except NoSuchTableError:
        pass

    data = pa.Table.from_pylist(
        [
            {
                "foo": "foo_val",
                "bar": 1,
                "baz": False,
                "qux": ["x", "y"],
                "quux": {"key": {"subkey": 2}},
                "location": [{"latitude": 1.1}],
                "person": {"name": "some_name", "age": 23},
            }
        ]
    )

    tbl = session_catalog.create_table_if_not_exists(identifier=identifier, schema=data.schema, properties=properties, partition_spec=partition_spec)

    tbl.append(data)

    return tbl


def test_table_statistics_from_iceberg_metadata():
    identifier = "default.iceberg_table_with_stats"
    if not catalog._namespace_exists('default'):
        catalog.create_namespace('default')

    table = _create_table(
        session_catalog=catalog,
        identifier = identifier
    )
    stats = table_statistics_from_iceberg_metadata(
        iceberg_table_io=table.io,
        iceberg_metadata=table.metadata
    )
    if stats.partition_count != 1:
        assert False
