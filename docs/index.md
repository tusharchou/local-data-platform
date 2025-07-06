---
title: Local Data Platform
---

```{include} ../README.md
```

## Recipes

See [Recipes for Local Data Platform](recipes.md) for practical usage examples, including reading JSON and building a JSON-to-Parquet pipeline.

## How to Read a JSON File

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
