import requests
import pyarrow.parquet as pq

#
#
# response = requests.get(
#     'https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet',
# )
#
#
# if response.status_code == 200:
#     with open("/tmp/yellow_tripdata_2023-01.parquet", "w") as f:
#         f.write(response.content)

df = pq.read_table("/tmp/yellow_tripdata_2023-01.parquet")

catalog.create_namespace("nyc_taxi")

table = catalog.create_table(
    "nyc_taxi.taxi_dataset",
    schema=df.schema,
)