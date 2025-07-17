# How to Set Up for Development

This guide explains how to set up your local environment for contributing to the `local-data-platform` project. Following these steps will ensure you have a consistent development environment that matches our CI pipeline.

## Prerequisites

Before you begin, ensure you have the following installed on your system:

-   **Git**: For version control.
-   **Python**: Version 3.12 or newer. You can check with `python --version`.
-   **Poetry**: For dependency management. We recommend installing it with `pipx` to avoid dependency conflicts.
    ```sh
    # Install pipx if you don't have it
    python -m pip install --user pipx
    python -m pipx ensurepath

    # Install poetry using pipx
    pipx install poetry
    ```

## Step 1: Clone the Repository

Clone the project from GitHub and navigate into the project directory:

```sh
git clone https://github.com/tusharchou/local-data-platform.git
cd local-data-platform
```

## Step 2: Install Dependencies

This project uses Poetry to manage dependencies. To install all packages required for development, testing, and building documentation, run:

```sh
poetry install --with dev,docs
```
This command will create a virtual environment within the project folder and install all the necessary libraries. You can activate it by running `poetry shell`.

## Step 3: Verify Your Setup

To ensure everything is configured correctly, run the linters, tests, and build the documentation.

### Run Linters
We use `flake8` to enforce code style. Check for any linting issues with:
```sh
poetry run flake8 src/ tests/
```

### Run Tests
Our tests are written with `pytest`. Execute the test suite by running:
```sh
poetry run pytest
```

### Build Documentation
The documentation is built with `MkDocs`. To preview the docs locally with live-reloading, run:
```sh
poetry run mkdocs serve
```
You can then open `http://127.0.0.1:8000` in your web browser. To perform a strict build like our CI process, use `poetry run mkdocs build --strict`.

You are now ready to contribute to `local-data-platform`!

## Troubleshooting

If you encounter issues, especially with dependencies, try a clean re-installation:

```sh
make reinstall
```