from .store.source.parquet import Parquet

def etl():
    print("trying to test that this library works")
    source = Parquet(
        name='nyc_taxi',
        path='/Users/tushar/Documents/GitHub/local-data-platform/local-data-platform/yellow_tripdata_2023-01.parquet',
        format='PARQUET'
    )
    print(f' source = {source}')
    print(f' metadata = {source.get().schema.metadata}')
