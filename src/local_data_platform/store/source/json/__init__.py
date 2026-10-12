"""JSON files, such as dataset configs and BigQuery query files, on local disk or an object store."""

import json
import os
from typing import Any

from pyarrow import fs as pafs

from local_data_platform import fs
from local_data_platform.exceptions import ConfigError
from local_data_platform.logger import get_logger
from local_data_platform.store.source import Source

logger = get_logger(__name__)


class Json(Source):
    """A JSON file on the local filesystem or an object store. It is read-only.

    Args:
        name: Dataset name.
        path: File path or URI. Absolute paths are kept; relative paths resolve against
            ``base_dir`` (or the cwd). See :func:`local_data_platform.paths.resolve_path`.
            ``file://``, ``s3://`` and ``gs://`` URIs are accepted; see :mod:`local_data_platform.fs`.
        base_dir: Folder that relative paths resolve against.
        **options: Extra options kept on ``self.options``.

    Attributes:
        path: The resolved location: an absolute ``Path`` for local files, the URI string for
            object stores.
    """

    def __init__(self, name: str, path: str | os.PathLike, base_dir: str | os.PathLike | None = None, **options):
        if not path:
            raise ConfigError(f"JSON dataset {name!r} needs a path")
        super().__init__(name, path=None, base_dir=base_dir, **options)
        self.path = fs.resolve_location(path, base_dir)

    def get(self) -> Any:
        """Parse the file. A ``.gz``, ``.bz2`` or ``.zst`` suffix is decompressed.

        Returns:
            The parsed JSON value.

        Raises:
            FileNotFoundError: If no file exists at the resolved path.
            json.JSONDecodeError: If the file is not valid JSON; the message names the file.
        """
        filesystem, path = fs.filesystem_for(self.path)
        if filesystem.get_file_info(path).type != pafs.FileType.File:
            raise FileNotFoundError(f"JSON file not found: {self.path}")
        logger.debug("Reading JSON %s", self.path)
        with filesystem.open_input_stream(path) as stream:
            text = stream.read().decode("utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise json.JSONDecodeError(f"{self.path} is not valid JSON: {exc.msg}", exc.doc, exc.pos) from exc


__all__ = ["Json"]
