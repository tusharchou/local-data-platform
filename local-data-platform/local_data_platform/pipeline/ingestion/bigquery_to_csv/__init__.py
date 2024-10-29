from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.store.source.json import Json
from local_data_platform.store.source.gcp.bigquery import BigQuery, GCPCredentials
from local_data_platform.format.csv import CSV
from local_data_platform.logger import log
import os
from pathlib import Path

logger = log()


class BigQueryToCSV(Ingestion):

    def __init__(self, config, *args, **kwargs):
        self.source_config = config.metadata["source"]
        print("source_config", self.source_config)
        self.target_config = config.metadata["target"]
        self.credentials_path = Path(
            os.getcwd() + self.source_config["credentials"]["path"]
        )
        self.credentials = GCPCredentials(
            path=self.credentials_path,
            kwargs=Json(
                name=self.source_config["credentials"]["name"],
                path=self.credentials_path,
            ).get(),
        )

        self.source = BigQuery(
            name=self.source_config["name"],
            credentials=self.credentials,
            path=self.source_config["path"],
        )
        self.target_path = Path(os.getcwd() + self.target_config["path"])
        self.target = CSV(name=self.target_config["name"], path=self.target_path)
        self.source_config_path = Path(os.getcwd() + self.source_config["path"])
        self.query = Json(
            name=self.source_config["name"], path=self.source_config_path
        ).get()["query"]

        logger.info(
            f"""
                BigQueryToCSV initialised with
                source {self.source}
                target {self.target}
                """
        )
        super().__init__(config, *args, **kwargs)

    def extract(self):
        return self.source.get(query=self.query)
