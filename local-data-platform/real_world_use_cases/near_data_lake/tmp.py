from pyspark.sql import SparkSession
import os
from pathlib import Path



config = {
    "name": "nyc_yellow_taxi_rides",    
    "path": "/Users/tushar/Documents/GitHub/local-data-platform/local-data-platform/yellow_tripdata_2023-01.parquet",
    "warehouse_path": "./tmp/warehouse",
    "catalog_identifier": "pyiceberg_catalog_db",
    "format": "PARQUET"
}

p=Path('C:/Users//singsina//Desktop//local-data-platform//local-data-platform//tmp//warehouse//')
print(p)
p_path="file:///"+str(p)
print(p_path)
# Initialize Spark session with Iceberg Hadoop catalog configuration
# spark = SparkSession.builder \
#     .appName("IcebergHadoopCatalogQuery") \
#     .config("spark.sql.catalog.my_catalog", "org.apache.iceberg.spark.SparkCatalog") \
#     .config("spark.sql.catalog.my_catalog.type", "hadoop") \
#     .config("spark.sql.catalog.my_catalog.warehouse", "file:///")\
#     .getOrCreate()

print(os.path.exists(p))

spark = SparkSession.builder \
                    .config("spark.sql.warehouse.dir", p) \
                    .enableHiveSupport() \
                    .appName("your-app-name") \
                    .getOrCreate()

