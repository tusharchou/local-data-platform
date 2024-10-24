from local_data_platform.pipeline import Pipeline


class Ingestion(Pipeline):


    def extract(self):
        self.source.get()

    def load(self):
        self.target.put(self.extract())
