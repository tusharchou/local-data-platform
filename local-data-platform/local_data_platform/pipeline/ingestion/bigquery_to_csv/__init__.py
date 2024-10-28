from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.store.source.gcp.bigquery import BigQuery
from local_data_platform.format.csv import CSV

logger = log()

class BigQueryToCsv(Ingestion):
        def __init__(self, config, *args, **kwargs):
            super().__init__(config, *args, **kwargs)
            self.source = config.metadata['source']
            self.target = config.metadata['target']
            self.source = BigQuery(
                name=self.source['name'],
                path=self.source['path']
            )
            self.target = CSV(
                name=self.target['name'],
                path=self.target['path']
            )
            logger.info(
                f"""
                BigQueryToCsv initialised with
                source {self.source}
                target {self.target}
                """
            )