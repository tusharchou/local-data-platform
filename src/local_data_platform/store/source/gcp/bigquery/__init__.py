"""Google BigQuery source.

The Google client libraries are an optional extra and are imported only when a
client is first needed::

    pip install "local-data-platform[bigquery]"

``BigQuery.get(query)`` runs the query exactly once and returns a
``pyarrow.Table``. Pass ``client=`` to supply your own ``google.cloud.bigquery.Client``
(or a test double); no Google import happens then.
"""

import importlib
import json
import os
from typing import Any

import pyarrow as pa

from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.logger import get_logger
from local_data_platform.store.source.gcp import GCP, GCPCredentials

logger = get_logger(__name__)

INSTALL_HINT = 'pip install "local-data-platform[bigquery]"'

__all__ = ["BigQuery", "GCPCredentials", "INSTALL_HINT"]


def _import_google() -> tuple[Any, Any]:
    """Import the Google BigQuery and service-account modules.

    Returns:
        The ``google.cloud.bigquery`` and ``google.oauth2.service_account`` modules.

    Raises:
        EngineNotFound: If either library is not installed.
    """
    try:
        bigquery = importlib.import_module("google.cloud.bigquery")
        service_account = importlib.import_module("google.oauth2.service_account")
    except ImportError as exc:
        raise EngineNotFound(
            f"BigQuery support needs the Google Cloud client libraries ({exc}). Install them with: {INSTALL_HINT}"
        ) from exc
    return bigquery, service_account


class BigQuery(GCP):
    """A read-only BigQuery source.

    Args:
        name: Name of the source, used in logs.
        credentials: Service-account credentials. May be ``None`` when ``client``
            is given.
        path: Optional path to a JSON file holding ``{"query": "..."}``. It is used
            by :meth:`get` when no query is passed.
        client: A ready ``google.cloud.bigquery.Client``. When omitted, one is built
            from ``credentials`` on first use.
        base_dir: Folder that a relative ``path`` resolves against.
    """

    def __init__(self, name: str, credentials: GCPCredentials | None, path: str | os.PathLike | None = None,
                 client: Any = None, base_dir: str | os.PathLike | None = None):
        self.credentials = credentials
        self.project_id = credentials.project_id if credentials is not None else None
        self._client = client
        super().__init__(name, path=path, base_dir=base_dir)
        logger.info("BigQuery source %r initialised (project_id=%s)", self.name, self.project_id)

    @property
    def client(self) -> Any:
        """The BigQuery client, built from ``credentials`` on first access.

        Raises:
            EngineNotFound: If the Google libraries are not installed.
            ConfigError: If no credentials were given or the key file is missing.
        """
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self) -> Any:
        bigquery, service_account = _import_google()
        if self.credentials is None:
            raise ConfigError(f"BigQuery source {self.name!r} needs credentials or a client")
        key_path = self.credentials.path
        if not key_path.is_file():
            raise ConfigError(f"GCP credentials file not found: {key_path}")
        google_credentials = service_account.Credentials.from_service_account_file(str(key_path))
        project = self.project_id or getattr(google_credentials, "project_id", None)
        logger.debug("Building BigQuery client for project %s from %s", project, key_path)
        return bigquery.Client(credentials=google_credentials, project=project)

    def _query_from_path(self) -> str:
        if self.path is None:
            raise ConfigError(f"BigQuery source {self.name!r} was given no query and has no query file path")
        if not self.path.is_file():
            raise ConfigError(f"BigQuery query file not found: {self.path}")
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"BigQuery query file {self.path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("query"), str):
            raise ConfigError(f"BigQuery query file {self.path} must hold an object with a 'query' string")
        return data["query"]

    def get(self, query: str | None = None) -> pa.Table:
        """Run ``query`` once and return its result.

        Args:
            query: The SQL to run. When ``None``, the ``query`` key of the JSON file
                at ``path`` is used.

        Returns:
            The query result as a ``pyarrow.Table``, from ``job.result().to_arrow()``.

        Raises:
            ConfigError: If there is no query to run.
            EngineNotFound: If a client has to be built and the Google libraries are
                not installed.
        """
        if query is None:
            query = self._query_from_path()
        if not isinstance(query, str) or not query.strip():
            raise ConfigError(f"BigQuery source {self.name!r} needs a non-empty query string")
        client = self.client
        logger.info("Running BigQuery query for source %r", self.name)
        logger.debug("BigQuery query for %r: %s", self.name, query)
        try:
            job = client.query(query)
            table = job.result().to_arrow()
        except Exception as exc:
            logger.error("BigQuery query for source %r failed: %s", self.name, exc)
            raise
        logger.info("BigQuery source %r returned %s rows", self.name, getattr(table, "num_rows", "?"))
        return table

    def put(self, *args, **kwargs):
        """BigQuery is a read-only source in this release.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("BigQuery is a read-only source; writing to BigQuery is not supported")
