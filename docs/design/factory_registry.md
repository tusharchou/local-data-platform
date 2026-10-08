# Design: pipeline registry and factory

Status: built in 0.1.1 (`local_data_platform/pipeline/registry.py`). This page replaces the draft
in PR #115, which described a different codebase.

## Problem

Before 0.1.1, every report script under `examples/` chose its pipeline class itself, with an
if/else on `SupportedFormat`. This is `examples/near_data_lake/reports/put_data.py` as it was:

```python
config = Config(**Json(name=dataset, path=config_path).get())

if (
    config.metadata["source"]["format"] == SupportedFormat.CSV.value
    and config.metadata["target"]["format"] == SupportedFormat.ICEBERG.value
):
    data_loader = CSVToIceberg(config=config)
    data_loader.load()
else:
    raise PipelineNotFound(
        f"source {config.metadata['source']['format']} "
        f"to target {config.metadata['target']['format']} pipeline is not supported yet"
    )
```

The other three scripts repeated the same block with a different pair of formats. That caused
four problems:

1. **Every caller owned the routing.** Adding a pipeline meant editing each script that might
   need it. The CLI would have needed a fifth copy.
2. **Callers had to know every class.** Each script imported concrete pipeline classes from deep
   module paths, so moving a class broke the scripts (see `pipeline.egression.csv_to_iceberg`
   versus `pipeline.ingestion.csv_to_iceberg`).
3. **The errors didn't help.** A missing pair raised `PipelineNotFound` saying it was "not
   supported yet", without saying which pairs *were* supported.
4. **The routing couldn't be tested.** It lived in top-level script code that ran on import, so
   no test could check that a config reached the right class.

The classes behind the if/else didn't agree on a constructor either: some took `config=`, some
built their own source and target from `os.getcwd()`, and `ParquetToIceberg` lived under
`ingestion` but inherited `Egression`. A single creation path is also the fix for that.

## Decision

Pipelines are chosen by a **registry** keyed by what a config declares, and built by one
**factory** function.

- **Key:** `(source_format, target_format, engine)`. The formats are `SupportedFormat` values.
  The engine is optional and only matters when two pipelines share a pair of formats: a JSON
  source with the `BIGQUERY` engine is a BigQuery query, not a local JSON file.
- **Registration:** each pipeline class registers itself with a decorator, next to its own code.

  ```python
  from local_data_platform.pipeline import Pipeline
  from local_data_platform.pipeline.registry import register_pipeline


  @register_pipeline("CSV", "ICEBERG")
  class CSVToIceberg(Pipeline):
      ...


  @register_pipeline("JSON", "CSV", engine="BIGQUERY")
  class BigQueryToCSV(Pipeline):
      ...
  ```

- **Factory:** `create_pipeline(config)` reads the source format, target format and engine from
  the config, looks up the class and returns an instance built from the config. Callers don't
  import pipeline classes at all:

  ```python
  from local_data_platform import Config
  from local_data_platform.pipeline.registry import create_pipeline

  result = create_pipeline(Config.from_json("config/egression.json")).run()
  ```

- **Failure:** an unregistered key raises `PipelineNotFound`, and the message lists the pairs that
  are registered, so the fix is visible in the error.
- **Introspection:** `registered_pipelines()` returns what is registered, and
  `get_pipeline_class(...)` returns the class for a key without building it. `ldp pipelines`
  prints the registered routes.
- **Built-ins:** `CSVToIceberg`, `ParquetToIceberg`, `IcebergToCSV`, `IcebergToParquet` and
  `BigQueryToCSV` must be registered before the first lookup, so `create_pipeline` works without
  the caller importing any pipeline module.

The pipelines themselves use composition: a `Pipeline` *has* a source, a target, a list of
transforms and a list of quality checks. The registry only decides which class turns a config
into those parts, so a new route is usually a small class that builds a different source or
target.

## What changed for callers

| Before 0.1.1 | From 0.1.1 |
|---|---|
| `Config(**Json(name=..., path="/real_world_use_cases/...").get())` | `Config.from_json("config/egression.json")` |
| An if/else on `SupportedFormat`, repeated in every script | `create_pipeline(config)` |
| `PipelineNotFound: ... not supported yet` | `PipelineNotFound` listing the registered pairs |
| A new pipeline means editing every caller | A new pipeline means one decorated class |

The four scripts in `examples/*/reports/` now contain no format checks, and `ldp run CONFIG`
uses the same factory.

## Adding a pipeline

To add, say, Parquet to CSV:

1. Write a `Pipeline` subclass that builds a Parquet source and a CSV target from the config.
2. Decorate it with `@register_pipeline("PARQUET", "CSV")`.
3. Make sure its module is imported with the built-ins, and add a test that
   `create_pipeline` returns it for a matching config.

No caller changes. `tests/test_examples.py` checks that every example config resolves to a
registered pipeline, so a config that names an unsupported route fails in CI, not at run time.

## Alternatives considered

- **Keep the if/else, but in one shared function.** This removes the copies, but the function still
  has to change for every new pipeline and still imports every class. The registry is the same
  lookup with an extension point.
- **A dict literal mapping pairs to classes.** This is simple, but it lives away from the classes
  and drifts from them. The decorator keeps registration next to the code it registers.
- **Plugin entry points** (`importlib.metadata`). These would let other packages add pipelines
  without touching this repo. That is more machinery than 0.1.1 needs; the registry's decorator
  API is what an entry-point loader would call, so it can be added later without breaking callers.
- **One factory class per engine** (abstract factory). There is one engine per route today, so a
  second level of factories would add indirection without removing any.

## Trade-offs

- **Registration happens at import.** A pipeline whose module is never imported isn't
  registered. The built-ins are imported for you; your own pipelines must be imported before you
  call `create_pipeline`.
- **The registry is global state.** Tests that register temporary pipelines must remove them
  afterwards with `unregister_pipeline(source_format, target_format, engine=None)`, or they leak
  into other tests. `register_pipeline(..., replace=True)` swaps a built-in on purpose; without
  `replace`, registering a different class for a route that is taken raises `ValueError`.
