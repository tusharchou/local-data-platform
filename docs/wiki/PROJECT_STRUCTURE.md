# Project Structure

```
tusharchou/local-data-platform/
├── local_data_platform/
│   ├── __init__.py
│   ├── storage_base.py
│   └── in_memory_storage.py
├── scripts/
│   ├── __init__.py
│   └── github_api.py
├── main.py
├── mkdocs.yml                <-- NEW FILE (MkDocs Configuration)
├── docs/                     <-- DIRECTORY
│   ├── index.md              <-- CHANGED (Formerly index.rst)
│   ├── problem_statement.md  <-- CHANGED (Formerly problem_statement.rst)
│   ├── installation.md       <-- Placeholder for new page
│   ├── usage.md              <-- Placeholder for new page
│   ├── api_reference.md      <-- Placeholder for new page (for mkdocstrings)
│   ├── contributing.md       <-- Placeholder for new page
│   └── community.md          <-- Placeholder for new page
├── requirements-dev.txt      <-- NEW FILE (For dev/docs dependencies)
└── README.md
```

## .gitignore

The following are ignored to keep the repo clean:
- IDE/project files (`.idea/`)
- Python cache (`__pycache__/`)
- Sphinx build output (`docs/_build/`)
- MkDocs static site output (`site/`)
