from local_data_platform.pipeline import Pipeline
from local_data_platform.logger import log

logger = log()


class Ingestion(Pipeline):

    def extract(self):
        logger.info("Extracting Source in ingestion pipeline")
        return self.source.get()

    def load(self):
        df = self.extract()
        logger.info(f"Loading Source {len(df)} in ingestion pipeline")
        self.target.put(df)
