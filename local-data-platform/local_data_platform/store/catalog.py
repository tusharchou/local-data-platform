from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
from . import Store

class LocalIceberg(Store):
    def __init__(self, catalog: str, *args, **kwargs):

        self.catalog = LocalIcebergCatalog(
            catalog['identifier'],
            path=catalog['warehouse_path']
        )
        super().__init__(*args, **kwargs)

    def set_identifier(self):
        return