"""The gold layer: tables that carry an owner, a meaning, a grain and column docs.

The table contract itself is :mod:`local_data_platform.contracts`. A pipeline writes it into its Iceberg table when it
publishes. This package holds the commands that work on the gold layer as a whole, and ``ldp gold sync`` is the first.
"""
