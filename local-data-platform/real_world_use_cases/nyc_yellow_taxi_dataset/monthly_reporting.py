'''
New York Taxi and Limousine Commission
TLC Trip Record Data

Yellow and green taxi trip records include fields capturing pick-up and drop-off dates/times,
pick-up and drop-off locations, trip distances, itemized fares, rate types, payment types,
and driver-reported passenger counts.

For-Hire Vehicle (“FHV”) trip records include fields capturing the dispatching base license number and the pick-up date,
time, and taxi zone location ID (shape file below).

All files will be stored in the PARQUET format.
Trip data will be published monthly (with two months delay) instead of bi-annually.
HVFHV files will now include 17 more columns (please see High Volume FHV Trips Dictionary for details).
Additional columns will be added to the old files as well.
'''
from local_data_platform.pipeline.ingestion.pyarrow.parquet_to_iceberg import ParquetToIceberg
from local_data_platform import Config, SupportedFormat
from local_data_platform.store.source.json import Json
from local_data_platform.exceptions import PipelineNotFound
import os
from logging import (
    getLogger,
    basicConfig,
    INFO
)


basicConfig(level=INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

logger = getLogger('nyc_yellow_taxi')


def put_nyc_yellow_taxi_dataset(
        dataset='nyc_taxi',
        config_path='/real_world_use_cases/nyc_yellow_taxi_dataset/config.json'
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
            path=os.getcwd()+config_path,
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