from local_data_platform.pipeline import Pipeline
from local_data_platform import Table


class Churn(Pipeline):


    def load(self) -> Table:
        return self.target.put()