from pyiceberg.catalog.sql import SqlCatalog

warehouse_path = "/tmp/warehouse"
catalog = SqlCatalog(
    "pyiceberg_catalog_db",
    **{
        "uri": f"sqlite:///{warehouse_path}/pyiceberg_catalog_db",
        "warehouse": f"file://{warehouse_path}",
    },
)

