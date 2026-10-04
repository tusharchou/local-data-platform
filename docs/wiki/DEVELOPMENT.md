# Development & Documentation

## Requirements

- Python 3.12 or newer
- `make`
- Optional: [scala-cli](https://scala-cli.virtuslab.org) for the Spark and REST catalog demos and
  tests. It provisions its own JDK.

## Setup

```sh
make install   # creates .venv and installs the package with the dev and docs extras
make help      # lists every target
```

## Running Tests

```sh
make lint      # flake8
make test      # pytest, offline; the Spark and REST integration tests skip
```

The Spark tests are opt-in with `make spark-test` (`LDP_RUN_SPARK=1`), and the REST catalog tests
with `make rest-test` (`LDP_RUN_REST=1`). Both need [scala-cli](https://scala-cli.virtuslab.org).

## Building & Serving Documentation

```sh
make docs        # mkdocs build --strict, the check CI runs
make serve-docs  # serves the docs on http://127.0.0.1:8000
```
