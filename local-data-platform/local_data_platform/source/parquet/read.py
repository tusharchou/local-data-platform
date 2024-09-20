import os
import  pyarrow.parquet as pq

SAMPLE_PARQUET = os.path.abspath('/Users/tushar/PycharmProjects/local-data-platform/local-data-platform/sample_data/nyc_yellow_taxi/parquet/2023-01/yellow_tripdata_2023-01.parquet')

# cwd_path = os.getcwd()
# print(type(cwd_path))
# print(cwd_path)
# print(SAMPLE_PARQUET)
# # print(path)
# print(__file__)
# print(os.path.dirname(__file__))
# print(os.path.realpath(os.path.dirname(__file__)))
# print(os.path.realpath(path))
# real_path = os.path.realpath(path)
# print("File exists:", os.path.exists(SAMPLE_PARQUET))
# print("File exists:", os.path.exists(cwd_path))
# print(cwd_path)
df = pq.read_table(SAMPLE_PARQUET)
#
print(df.get_total_buffer_size())