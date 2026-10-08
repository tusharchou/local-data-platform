# Contributing to Local Data Platform

We're thrilled that you're interested in contributing to the Local Data Platform! Your help is essential for keeping it great.

This section provides guidelines for contributing to the project. Please take a moment to review this document in order to make the contribution process easy and effective for everyone involved.

## How to Get Started

If you're new to the project, set up a development environment from a clone of the repository. You need Python 3.12 or newer:

```sh
git clone https://github.com/tusharchou/local-data-platform.git
cd local-data-platform
make install   # creates .venv and installs the package with the dev and docs extras
make test      # runs the test suite
make demo      # runs the end-to-end demo into ./ldp_demo
```

`make help` lists every target. The [Quickstart](quickstart.md) explains how the library works.

Once you're set up, you can explore the other pages in this section to learn how to report issues or request features.

## Submitting Pull Requests

We follow a standard "fork and pull" model for contributions. To submit a change, please follow these steps:

1.  **Create a Fork**: Fork the repository to your own GitHub account.
2.  **Create a Branch**: Create a new branch from `main` in your fork for your changes. Please use a descriptive branch name (e.g., `feat/add-new-ingestion-source` or `fix/docs-build-error`).
3.  **Make Your Changes**: Make your changes, ensuring you follow the project's coding style.
4.  **Run Quality Checks**: Before committing, run all the local quality checks to ensure your changes don't introduce any issues. `make all` runs lint, the tests, the strict docs build, the wheel build and the smoke test, the same checks as CI.
    ```sh
    make all
    ```
5.  **Commit Your Changes**: Commit your changes with a clear and descriptive commit message. We follow the Conventional Commits specification.
6.  **Push to Your Fork**: Push your branch to your fork on GitHub.
7.  **Open a Pull Request**: From your fork on GitHub, open a pull request to the `main` branch of the `tusharchou/local-data-platform` repository.

Your PR will be reviewed by the maintainers, and once approved, it will be merged. Thank you for your contribution!