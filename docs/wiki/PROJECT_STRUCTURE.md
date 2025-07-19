# Project Structure

```
local-data-platform/
├── local-data-platform/      # Main Python package
├── docs/                    # Documentation (Markdown)
├── tests/                   # Unit and integration tests
├── .github/                 # GitHub Actions workflows
├── .readthedocs.yaml        # Read the Docs config
├── mkdocs.yml               # MkDocs config
├── pyproject.toml           # Poetry config
└── ...
```

## .gitignore

The following are ignored to keep the repo clean:
- IDE/project files (`.idea/`)
- Python cache (`__pycache__/`)
- Sphinx build output (`docs/_build/`)
- MkDocs static site output (`site/`)
