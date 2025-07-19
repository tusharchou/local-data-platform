# Reviewing Pull Requests

This page provides an overview of significant pull requests that are ready for review. It helps project managers and senior developers track progress and ensure quality.

---

## Example PR: Documentation and Testing Improvements

This is an example of a pull request description to guide reviews.

### Summary
This pull request enhances the documentation and testing for the `local-data-platform` project. The main improvements include:

- Adding a `recipes.md` page to the documentation, featuring practical usage examples such as reading a JSON file and building a JSON-to-Parquet pipeline.
- Ensuring the recipes page appears in the sidebar/main navigation for easier access.
- Updating Sphinx and Markdown documentation structure for improved navigation and clarity.
- Adding a test (`tests/test_json_source.py`) to verify that the `JsonSource` class can read a JSON file as described in the documentation.
- Maintaining compatibility for documentation builds both locally and on Read the Docs.

### How to Test
- Build the documentation locally:
  ```sh
  cd docs
  make html
  ```
  Verify that the "Recipes" page appears in the sidebar and renders correctly.

- Run the test suite to ensure the new test passes:
  ```sh
  pytest tests/test_json_source.py
  ```