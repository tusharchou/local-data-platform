from local_data_platform import Config, SupportedFormat
from local_data_platform.store.source.json import Json
from local_data_platform.exceptions import PipelineNotFound
import os
from local_data_platform.logger import log
from local_data_platform.pipeline.egression.iceberg_to_csv import IcebergToCSV


logger = log()


def get_nyc_yellow_taxi_dataset(
        dataset='nyc_taxi',
        config_path='/real_world_use_cases/nyc_yellow_taxi_dataset/config/egression.json'
):
    logger.info(
        """
        We will try to read a ICEBERG table from local catalog 
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
            config.metadata['source']['format'] == SupportedFormat.ICEBERG.value and
            config.metadata['target']['format'] == SupportedFormat.CSV.value
    ):
        data_loader = IcebergToCSV(config=config)
        data_loader.load()
    else:
        raise PipelineNotFound(
            f"""
            source {config.metadata['source']['format']} 
            to target {config.metadata['target']['format']}
            pipeline is not supported yet
            """
        )


get_nyc_yellow_taxi_dataset()