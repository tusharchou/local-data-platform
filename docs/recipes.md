---
title: Recipes for Local Data Platform
---

# Local Data Platform Recipes

This page contains practical recipes for using the local-data-platform library.

## Recipe: Read a JSON File

You can read a JSON file using the `local-data-platform` library as follows:

```python
from local_data_platform.store.source.json import JsonSource

# Initialize the JSON source with your file path
json_source = JsonSource(path="path/to/your/file.json")

# Read the data
# (use .get() or .read() depending on your implementation)
data = json_source.get()

print(data)
```

> Replace `path/to/your/file.json` with the path to your JSON file.
> The exact class/method may differ depending on your implementation.

---

## Recipe: JSON to Parquet Data Pipeline

This example demonstrates how to build a simple pipeline that reads data from a JSON file and writes it to a Parquet file using the local-data-platform library.

```python
from local_data_platform.store.source.json import JsonSource
from local_data_platform.store.target.parquet import ParquetTarget
from local_data_platform.pipeline.ingestion.json_to_parquet import JsonToParquetPipeline

# Initialize source and target
json_source = JsonSource(path="data/input.json")
parquet_target = ParquetTarget(path="data/output.parquet")

# Create and run the pipeline
pipeline = JsonToParquetPipeline(source=json_source, target=parquet_target)
pipeline.run()
```

> Make sure to replace the class names and import paths with the actual ones from your implementation.
> This assumes you have a pipeline class like `JsonToParquetPipeline`.

---

## Sample JSON File

```json
[
  {"id": 1, "name": "Alice", "score": 95},
  {"id": 2, "name": "Bob", "score": 88},
  {"id": 3, "name": "Charlie", "score": 92}
]
```

Save this as `data/input.json` to test the pipeline.
