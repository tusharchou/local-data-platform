from local_data_platform.pipeline.ingestion.csv_to_iceberg import CSVToIceberg
from local_data_platform import Config, SupportedFormat
from local_data_platform.store.source.json import Json
from local_data_platform.exceptions import PipelineNotFound
import os
from local_data_platform.logger import log


logger = log()

# Get the absolute path to the directory of the current script
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Construct the default config path relative to the script's location
_DEFAULT_CONFIG_PATH = os.path.normpath(
    os.path.join(_SCRIPT_DIR, "..", "config", "egression.json")
)

def put_near_transaction_dataset(
    dataset="near_transactions",
    config_path=_DEFAULT_CONFIG_PATH,
):
    """
    Loads and processes a dataset based on the provided configuration.

    This function reads a configuration file and uses it to load and process
    a dataset. It currently supports loading data from CSV format and converting
    it to Iceberg format.

    Args:
        dataset (str): The name of the dataset to be processed. Defaults to "near_transactions".
        config_path (str): The path to the configuration file. Defaults to the `egression.json` file within the module's `config` directory.

    Raises:
        PipelineNotFound: If the source and target formats specified in the configuration are not supported.
    """
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

    source_meta = config.metadata["source"]
    target_meta = config.metadata["target"]

    # Check for the specific supported pipeline
    is_csv_to_iceberg = (
        source_meta["format"] == SupportedFormat.CSV.value
        and target_meta["format"] == SupportedFormat.ICEBERG.value
    )

    if is_csv_to_iceberg:
        data_loader = CSVToIceberg(config=config)
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
    put_near_transaction_dataset()
