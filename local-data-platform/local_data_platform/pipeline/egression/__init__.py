from local_data_platform.pipeline import Pipeline
from local_data_platform.logger import log


logger = log()

class Egression(Pipeline):


    def extract(self):
        logger.info(
            f"""
            Extracting data for Egression from {self.source}
            """
        )
        return self.source.get()

    def load(self):
        logger.info(
            f"""
            Loading data for Egression to {self.target}
            """
        )
        self.target.put(self.extract())
