from pyiceberg.catalog.sql import SqlCatalog


class LocalIcebergCatalog(SqlCatalog):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
