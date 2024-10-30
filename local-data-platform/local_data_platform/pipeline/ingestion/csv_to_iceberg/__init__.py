from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.logger import log

logger = log()


class CSVToIceberg(Ingestion):
        """
        CSVToIceberg is a class responsible for ingesting data from a CSV source and 
        loading it into an Iceberg target.

        Attributes:
            source (CSV): The source CSV configuration.
            target (Iceberg): The target Iceberg configuration.

        Methods:
            __init__(config, *args, **kwargs):
                Initializes the CSVToIceberg instance with the provided configuration.
                Logs the initialization details of the source and target.
        
        Args:
            config (Config): Configuration object containing metadata for source and target.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.
        """    
        def __init__(self, config, *args, **kwargs):
            self.source = config.metadata['source']
            self.target = config.metadata['target']
            self.source = CSV(
                name=self.source['name'],
                path=self.source['path']
            )
            logger.info(
                f"""
                CSVToIceberg initialised with
                source {self.source}
                """
            )
            self.target = Iceberg(
                name=self.target['name'],
                config=self.target['catalog']
            )
            logger.info(
                f"""
                CSVToIceberg initialised with
                source {self.source}
                target {self.target}
                """
            )
            super().__init__(config, *args, **kwargs)