"""Integrations: ways to run local-data-platform from other tools.

* :mod:`~local_data_platform.integrations.airflow`: :class:`LdpRunOperator`, an Apache Airflow 3 operator that runs a
  dataset config for the data interval of a DAG run. Airflow is imported when the operator is used, so the module and
  its mapping functions need no Airflow.
"""
