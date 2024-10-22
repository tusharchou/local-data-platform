from .store.source.json import Json
from .pipeline.ingestion.pyarrow import PyArrow
from . import Config
import os


def etl():
    print("trying to test that this library works")

    config = Config(
        **Json(
            name='nyc_taxi',
            path=os.getcwd()+'/real_world_use_cases/nyc_yellow_taxi_dataset/config.json',
            format='JSON'
        ).get()
    )
    # print(f" config.metadata {config.metadata['source']}")
    etl = PyArrow(config)
    # df = etl.extract()
    # etl.load()
    local = etl.target.catalog
    for db in local.get_dbs():
        for table in local.get_tables(db):
            print(local.get_table(table).scan(limit=1).to_arrow())
            input()
