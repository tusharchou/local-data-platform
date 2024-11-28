# the row count is a common feature in formats like Parquet and Iceberg
import math
from io import BytesIO
from typing import List, Optional, Dict, Union, Tuple

import pandas
from pyarrow import parquet
import pyarrow as pa
from pyarrow import (
    parquet as pq
)
from pandas import DataFrame, read_parquet
from pyiceberg.table import Table, TableMetadata, DataScan
from pyiceberg.catalog.sql import SqlCatalog

warehouse_path = "sample_data/warehouse"
catalog = SqlCatalog(
    "default",
    **{
        "uri": f"sqlite:///{warehouse_path}/pyiceberg_catalog.db",
        "warehouse": f"file://{warehouse_path}",
    },
)


def _create_data() -> DataFrame:
    """
    to test that any table counts can be calculated from just metadata
    we will create a test data List that we can use to test
    """

    example_row = [
        "nyc,1,1,1"
    ]
    example_data_dictionary = [
        "place",
        "user_id",
        "vendor_id",
        "listings_id"
    ]
    # example_data = [column:example_row.split for index,column in enumerate(example_data_dictionary)]
    dict_of_column_values = {
        "place" : ["nyc", "blr"],
        "user_id": [ 1, 2],
        "vendor_id": [ 101, 102],
        "listings_id": [ 1001, 1002]
    }
    return DataFrame(dict_of_column_values)

def _create_parquet_metadata() -> parquet.FileMetaData:
    df  = _create_data()
    buf_bytes = df.to_parquet()

    buf_stream = BytesIO(buf_bytes)
    parquet_file = parquet.ParquetFile(buf_stream)

    return parquet_file.metadata


    # parquet_metadata = read_parquet(buf_stream)
    def construct_test_table(
            write_statistics: Union[bool, List[str]] = True,
    ) -> Tuple[pq.FileMetaData, Union[TableMetadataV1, TableMetadataV2]]:
        table_metadata = {
            "format-version": 2,
            "location": "s3://bucket/test/location",
            "last-column-id": 7,
            "current-schema-id": 0,
            "schemas": [
                {
                    "type": "struct",
                    "schema-id": 0,
                    "fields": [
                        {"id": 1, "name": "strings", "required": False, "type": "string"},
                        {"id": 2, "name": "floats", "required": False, "type": "float"},
                        {
                            "id": 3,
                            "name": "list",
                            "required": False,
                            "type": {"type": "list", "element-id": 6, "element": "long", "element-required": False},
                        },
                        {
                            "id": 4,
                            "name": "maps",
                            "required": False,
                            "type": {
                                "type": "map",
                                "key-id": 7,
                                "key": "long",
                                "value-id": 8,
                                "value": "long",
                                "value-required": False,
                            },
                        },
                        {
                            "id": 5,
                            "name": "structs",
                            "required": False,
                            "type": {
                                "type": "struct",
                                "fields": [
                                    {"id": 9, "name": "x", "required": False, "type": "long"},
                                    {"id": 10, "name": "y", "required": False, "type": "float", "doc": "comment"},
                                ],
                            },
                        },
                    ],
                },
            ],
            "default-spec-id": 0,
            "partition-specs": [{"spec-id": 0, "fields": []}],
            "properties": {},
        }

        table_metadata = TableMetadataUtil.parse_obj(table_metadata)
        arrow_schema = schema_to_pyarrow(table_metadata.schemas[0])

        _strings = ["zzzzzzzzzzzzzzzzzzzz", "rrrrrrrrrrrrrrrrrrrr", None, "aaaaaaaaaaaaaaaaaaaa"]

        _floats = [3.14, math.nan, 1.69, 100]

        _list = [[1, 2, 3], [4, 5, 6], None, [7, 8, 9]]

        _maps: List[Optional[Dict[int, int]]] = [
            {1: 2, 3: 4},
            None,
            {5: 6},
            {},
        ]

        _structs = [
            asdict(TestStruct(1, 0.2)),
            asdict(TestStruct(None, -1.34)),
            None,
            asdict(TestStruct(54, None)),
        ]

        table = pa.Table.from_pydict(
            {
                "strings": _strings,
                "floats": _floats,
                "list": _list,
                "maps": _maps,
                "structs": _structs,
            },
            schema=arrow_schema,
        )
        metadata_collector: List[Any] = []

        with pa.BufferOutputStream() as f:
            with pq.ParquetWriter(
                    f, table.schema, metadata_collector=metadata_collector, write_statistics=write_statistics
            ) as writer:
                writer.write_table(table)

        return metadata_collector[0], table_metadata


def test_parquet_count():
    metadata = _create_parquet_metadata()
    assert metadata.num_rows == 2

def _create_iceberg_metadata() -> DataScan:
    df = _create_data()
    arrow_table = pa.Table.from_pandas(df)
    # catalog.create_namespace("default")

    # table = catalog.create_table(
    #     "default.taxi_dataset",
    #     schema=arrow_table.schema,
    # )
    table = catalog.load_table("default.taxi_dataset")
    table.append(arrow_table)
    return DataScanV2(table.scan())

def test_arrow_count():
    df = _create_data()
    arrow_table = pa.Table.from_pandas(df)
    assert arrow_table.num_rows == 2

def test_iceberg_count():
    table = _create_iceberg_metadata()
    assert len(table.to_arrow()) == 2

class DataScanV2():
    def __init__(self, scan: DataScan):
        self.scan = scan
    def count(self):
        res = 0
        tasks = self.scan.plan_files()
        for task in tasks:
            res+=task.file.record_count
        return res

def test_iceberg_metadata_only_count():
    table = _create_iceberg_metadata()
    assert table.count() == 2
