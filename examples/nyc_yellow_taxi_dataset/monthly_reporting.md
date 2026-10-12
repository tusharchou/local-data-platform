# NYC TLC trip record data

The New York City Taxi and Limousine Commission (TLC) publishes trip records at
<https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page>.

- Yellow and green taxi records hold pick-up and drop-off times and locations, trip
  distance, itemised fares, rate and payment types, and the passenger count the
  driver reported.
- For-Hire Vehicle (FHV) records hold the dispatching base licence number, the
  pick-up time and the taxi zone ID.
- Files are Parquet, published monthly with a delay of about two months.

This example loads one month of yellow-taxi trips (`yellow_tripdata_2023-01.parquet`).
See [../README.md](../README.md) for how to download it and run the pipelines.
