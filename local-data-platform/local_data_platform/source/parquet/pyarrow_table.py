from pyarrow import parquet

class PyarrowTable():



    def from_parquet(self, path):
        self.df = parquet.read_table(path)

        return self.df