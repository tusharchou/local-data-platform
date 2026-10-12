"""Google Cloud Platform sources.

:class:`GCPCredentials` points at a service-account key file. It keeps only the
file's path and the ``project_id``: the key material itself is never stored on the
object, logged or included in ``repr``. The Google client libraries read the key
file directly when a client is built (see
:class:`local_data_platform.store.source.gcp.bigquery.BigQuery`).
"""

import json
import os
from pathlib import Path
from typing import Any

from local_data_platform import Credentials
from local_data_platform.exceptions import ConfigError
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path
from local_data_platform.store.source import Source

logger = get_logger(__name__)


def _read_project_id(path: Path) -> str | None:
    """Return ``project_id`` from a service-account key file.

    Args:
        path: Resolved path of the key file.

    Returns:
        The ``project_id`` value, or ``None`` when the file has none.

    Raises:
        ConfigError: If the file is missing or is not a JSON object. The message names
            the path only, never the file contents.
    """
    if not path.is_file():
        raise ConfigError(f"GCP credentials file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # The exception type is enough to diagnose; its text could quote the file.
        raise ConfigError(f"GCP credentials file {path} could not be read as JSON ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise ConfigError(f"GCP credentials file {path} must hold a JSON object")
    return data.get("project_id")


class GCPCredentials(Credentials):
    """Service-account credentials for Google Cloud.

    Only the key file's path and its ``project_id`` are kept. The private key and
    the other key fields are never stored, logged or shown in ``repr``.

    Args:
        path: Path to the service-account JSON key file. Relative paths resolve
            against ``base_dir`` (or the cwd), following
            :func:`local_data_platform.paths.resolve_path`.
        kwargs: The parsed key file, for callers (such as pre-0.1.1 pipelines) that
            have already read it. Only its ``project_id`` is used. When ``None``,
            the key file is read to find the ``project_id``.
        base_dir: Folder that a relative ``path`` resolves against.

    Raises:
        ConfigError: If ``kwargs`` is ``None`` and the key file is missing or is
            not valid JSON.
    """

    def __init__(self, path: str | os.PathLike, kwargs: dict[str, Any] | None = None,
                 base_dir: str | os.PathLike | None = None):
        resolved = resolve_path(path, base_dir)
        if kwargs is None:
            project_id = _read_project_id(resolved)
        else:
            project_id = kwargs.get("project_id")
            if not project_id and resolved.is_file():
                project_id = _read_project_id(resolved)
        if not project_id:
            logger.warning("GCP credentials at %s have no project_id; the client default project will be used",
                           resolved)
        super().__init__(path=resolved, project_id=project_id)
        logger.info("GCP credentials loaded from %s (project_id=%s)", self.path, self.project_id)

    def get_project_id(self) -> str | None:
        """Return the ``project_id`` of these credentials.

        Kept for pre-0.1.1 callers; it no longer re-reads the key file.
        """
        return self.project_id

    def __repr__(self) -> str:
        return f"GCPCredentials(path={str(self.path)!r}, project_id={self.project_id!r}, key=<redacted>)"

    __str__ = __repr__


class GCP(Source):
    """Base class for Google Cloud sources.

    Args:
        name: Name of the source.
        path: Optional path associated with the source, such as a query file.
        base_dir: Folder that a relative ``path`` resolves against.
    """

    def __init__(self, name: str, path: str | os.PathLike | None = None,
                 base_dir: str | os.PathLike | None = None, **options):
        super().__init__(name, path=path, base_dir=base_dir, **options)
        logger.debug("%s source %r initialised with path %s", type(self).__name__, self.name, self.path)
