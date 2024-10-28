from local_data_platform.catalog import Catalog


class LocalCatalog(Catalog):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
