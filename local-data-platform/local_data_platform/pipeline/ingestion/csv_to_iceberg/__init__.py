from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg

logger = log()

class CsvToIceberg(Ingestion):
        def __init__(self, config, *args, **kwargs):
            super().__init__(config, *args, **kwargs)
            self.source = config.metadata['source']
            self.target = config.metadata['target']
            self.source = CSV(
                name=self.source['name'],
                path=self.source['path']
            )
            self.target = Iceberg(
                name=self.target['name'],
                catalog=self.target['catalog']
            )
            logger.info(
                f"""
                CsvToIceberg initialised with
                source {self.source}
                target {self.target}
                """
            )