"""The gold layer: tables that carry an owner, a meaning, a grain and column docs.

The table contract itself is :mod:`local_data_platform.contracts`. A pipeline writes it into its Iceberg table when it
publishes. This package holds the commands that work on the gold layer as a whole. ``ldp gold sync`` writes a contract
into a table, ``build`` builds the gold tables of a folder and cuts a release, and ``list``, ``show`` and ``docs`` are
the catalog. ``export`` writes one table as a CSV, Parquet, JSONL or Excel file, and ``duckdb`` writes the whole
layer as one DuckDB file. The modules are ``sync``, ``build``, ``release``, ``catalog`` (the entries), ``render``
(text, a page and Markdown), ``status`` (the runs and the quality of a table), ``serve`` (the files) and ``cli``.
"""
