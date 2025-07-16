from local_data_platform.pipeline.ingestion.bigquery_to_csv import BigQueryToCSV
from local_data_platform import Config, SupportedFormat, SupportedEngine
from local_data_platform.store.source.json import Json
from local_data_platform.exceptions import PipelineNotFound
import os
from local_data_platform.logger import log


logger = log()

# Get the absolute path to the directory of the current script
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Construct the default config path relative to the script's location
_DEFAULT_CONFIG_PATH = os.path.normpath(
    os.path.join(_SCRIPT_DIR, "..", "config", "ingestion.json")
)


def get_near_transaction_dataset(
    dataset="near_transactions",
    config_path=_DEFAULT_CONFIG_PATH,
):
    """
    Retrieves and processes the near transaction dataset based on the provided configuration.

    Args:
        dataset (str): The name of the dataset to be processed. Defaults to "near_transactions".
        config_path (str): The path to the configuration file. Defaults to the `ingestion.json` file within the module's `config` directory.

    Raises:
        PipelineNotFound: If the source and target formats specified in the configuration are not supported.

    Returns:
        None
    """

    config = Config(
        **Json(
            name=dataset,
            path=config_path,
        ).get()
    )
    print(config)
    logger.info(
        f"""
        We are using the following dictionary as the configuration to generate a monthly trust metric
        {config}
        """
    )

    source_meta = config.metadata["source"]
    target_meta = config.metadata["target"]

    # Check for the specific supported pipeline
    is_bigquery_to_csv = (
        source_meta["format"] == SupportedFormat.JSON.value
        and target_meta["format"] == SupportedFormat.CSV.value
        and source_meta["engine"] == SupportedEngine.BIGQUERY.value
    )

    if is_bigquery_to_csv:
        data_loader = BigQueryToCSV(config=config)
        data_loader.load()
    else:
        raise PipelineNotFound(
            f"""
            source {config.metadata['source']['format']} 
            to target {config.metadata['target']['format']}
            pipeline is not supported yet
            """
        )


if __name__ == "__main__":
    get_near_transaction_dataset()
