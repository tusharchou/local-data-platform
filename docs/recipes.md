# Recipes

This section contains practical examples and step-by-step guides for using the Local Data Platform.

---

## Reading a Local JSON File

This recipe demonstrates how to use a `JsonSource` to read a local JSON file into a data structure.

### Prerequisites

- A local JSON file named `data.json` in your project directory.

### Code Example

```python
from local_data_platform.store import JsonSource

def read_local_json(file_path: str):
    json_source = JsonSource(path=file_path)
    data = json_source.read()
    print("Successfully read data:", data)

if __name__ == "__main__":
    read_local_json("data.json")
```