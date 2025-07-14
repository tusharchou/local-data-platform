from local_data_platform import Config, SupportedFormat
from local_data_platform.store.source.json import Json
from local_data_platform.exceptions import PipelineNotFound
import os
from local_data_platform.logger import log
from local_data_platform.pipeline.ingestion.parquet_to_iceberg import ParquetToIceberg

logger = log()


def put_nyc_yellow_taxi_dataset(
        dataset='nyc_taxi',
        config_path='/real_world_use_cases/nyc_yellow_taxi_dataset/config/ingestion.json'
):
    logger.info(
        """
        We will try to read a PARQUET file downloaded from :
        https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page
        """
    )

    config = Config(
        **Json(
            name=dataset,
            path=config_path,
        ).get()
    )

    logger.info(
        f"""
        We are using the following dictionary as the configuration to generate a monthly trust metric
        {config}
        """
    )
    if (
            config.metadata['source']['format'] == SupportedFormat.PARQUET.value and
            config.metadata['target']['format'] == SupportedFormat.ICEBERG.value
    ):
        data_loader = ParquetToIceberg(config=config)
        data_loader.load()
    else:
        raise PipelineNotFound(
            f"""
            source {config.metadata['source']['format']} 
            to target {config.metadata['target']['format']}
            pipeline is not supported yet
            """
        )


put_nyc_yellow_taxi_dataset()
