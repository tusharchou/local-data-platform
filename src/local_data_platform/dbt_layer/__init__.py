"""dbt over LDP's Iceberg tables.

LDP registers its Iceberg tables as sources in the DuckDB file that dbt builds in, dbt runs as a
subprocess, and the models marked ``ldp_publish`` are written back to Iceberg. The design is in
``docs/design/warehousing.md``. The package is named ``dbt_layer`` so it never shadows the ``dbt``
package, as ``mcp_server`` never shadows ``mcp``. Importing it loads neither dbt nor DuckDB.
"""

from local_data_platform.dbt_layer.sources import (
    RegisteredSource,
    SourceRegistration,
    register_sources,
    sources_yaml,
)

__all__ = ["RegisteredSource", "SourceRegistration", "register_sources", "sources_yaml"]
