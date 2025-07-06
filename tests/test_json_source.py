import json
from local_data_platform.store.source.json import JsonSource
import tempfile
import os

def test_json_source_reads_file():
    # Create a sample JSON file
    sample_data = [
        {"id": 1, "name": "Alice", "score": 95},
        {"id": 2, "name": "Bob", "score": 88},
        {"id": 3, "name": "Charlie", "score": 92}
    ]
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False) as tmp:
        json.dump(sample_data, tmp)
        tmp_path = tmp.name

    try:
        # Use JsonSource to read the file
        src = JsonSource(path=tmp_path)
        data = src.get()  # or src.read() if that's the method
        assert data == sample_data
    finally:
        os.remove(tmp_path)
