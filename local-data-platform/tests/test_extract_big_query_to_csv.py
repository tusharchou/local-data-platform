from local_data_platform.pipeline.ingestion.bigquery_to_csv import BigQueryToCSV
from local_data_platform import Config, SupportedFormat, SupportedEngine
from local_data_platform.store.source.json import Json
from local_data_platform.exceptions import PipelineNotFound
from local_data_platform.logger import log
import pytest


logger = log()


class TestBigQueryToCSV:

    def test_config(
            self,
            dataset="near_transactions",
            config_path="/sample_data" + "/real_world_use_cases/near_data_lake/config/ingestion.json",
    ):

        config = Config(
            **Json(
                name=dataset,
                path=config_path,
            ).get()
        )
        if (
                config.metadata["source"]["format"] == SupportedFormat.JSON.value
                and config.metadata["target"]["format"] == SupportedFormat.CSV.value
                and config.metadata["source"]["engine"] == SupportedEngine.BIGQUERY.value
        ):
            data_loader = BigQueryToCSV(config=config)
            data_loader.load()

            assert True
        else:
            raise AssertionError(
                f"""
                    source {config.metadata['source']['format']} 
                    to target {config.metadata['target']['format']}
                    pipeline is not supported yet
                    """
            )
    # def test_extract(self):
    #     def test_schema(table_v2: Table) -> None:
    #         assert table_v2.schema() == Schema(
    #             NestedField(field_id=1, name="x", field_type=LongType(), required=True),
    #             NestedField(field_id=2, name="y", field_type=LongType(), required=True, doc="comment"),
    #             NestedField(field_id=3, name="z", field_type=LongType(), required=True),
    #             identifier_field_ids=[1, 2],
    #         )
    #         assert table_v2.schema().schema_id == 1
    #
    #     assert False
