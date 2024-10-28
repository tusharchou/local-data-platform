from local_data_platform.pipeline.egression.csv_to_iceberg import CsvToIceberg
from local_data_platform.pipeline.ingestion.bigquery_to_csv import BigQueryToCsv
from local_data_platform import Config, SupportedFormat, SupportedEngine
from local_data_platform.store.source.json import Json
from local_data_platform.exceptions import PipelineNotFound
import os
from local_data_platform.logger import log


logger = log()


def get_near_trasaction_dataset(
        dataset='near_transactions',
        config_path='/real_world_use_cases/near_data_lake/config/ingestion.json'
):

    config = Config(
        **Json(
            name=dataset,
            path=os.getcwd()+config_path,
        ).get()
    )
    print(config)
    logger.info(
        f"""
        We are using the following dictionary as the configuration to generate a monthly trust metric
        {config}
        """
    )
    if (
            config.metadata['source']['format'] == SupportedFormat.JSON.value and
            config.metadata['target']['format'] == SupportedFormat.CSV.value and 
            config.metadata['source']['engine'] == SupportedEngine.BIGQUERY.value
    ):
        data_loader = BigQueryToCsv(config=config)
        data_loader.load()
    else:
        raise PipelineNotFound(
            f"""
            source {config.metadata['source']['format']} 
            to target {config.metadata['target']['format']}
            pipeline is not supported yet
            """
        )

def put_near_trasaction_dataset(
        dataset='near_transactions',
        config_path='/real_world_use_cases/near_data_lake/config/egression.json'
):
    
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
            config.metadata['source']['format'] == SupportedFormat.CSV.value and
            config.metadata['target']['format'] == SupportedFormat.ICEBERG.value
    ):
        data_loader = CsvToIceberg(config=config)
        data_loader.load()
    else:
        raise PipelineNotFound(
            f"""
            source {config.metadata['source']['format']} 
            to target {config.metadata['target']['format']}
            pipeline is not supported yet
            """
        )  


#get_near_trasaction_dataset();
put_near_trasaction_dataset();    