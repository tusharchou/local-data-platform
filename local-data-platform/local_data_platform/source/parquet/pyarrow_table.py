from pyarrow import Table, parquet

class PyarrowTable(Table):


    def get_from_parquet(self, path):
        self.df = parquet.read_table(path)