"""Parquet files read and written with ``pyarrow.parquet``, on local disk or an object store."""

import os

import pyarrow as pa
from pyarrow import fs as pafs
from pyarrow import parquet

from local_data_platform import fs
from local_data_platform.exceptions import ConfigError
from local_data_platform.format import Format
from local_data_platform.logger import get_logger

logger = get_logger(__name__)


class Parquet(Format):
    """A Parquet file (or a folder of Parquet files) on the local filesystem or an object store.

    Args:
        name: Dataset name.
        path: File or folder path, or URI. Absolute paths are kept; relative paths resolve
            against ``base_dir`` (or the cwd). See :func:`local_data_platform.paths.resolve_path`.
            ``file://``, ``s3://`` and ``gs://`` URIs are accepted, and on an object store a
            key prefix works as a folder; see :mod:`local_data_platform.fs`.
        base_dir: Folder that relative paths resolve against, usually the config file's folder.
        **options: Extra options kept on ``self.options``.

    Attributes:
        path: The resolved location: an absolute ``Path`` for local files, the URI string for
            object stores.
    """

    def __init__(self, name: str, path: str | os.PathLike, base_dir: str | os.PathLike | None = None, **options):
        if not path:
            raise ConfigError(f"Parquet dataset {name!r} needs a path")
        super().__init__(name, path=None, base_dir=base_dir, **options)
        self._set_location(path, base_dir)

    def get(self) -> pa.Table:
        """Read the file, or every Parquet file under the folder or key prefix.

        Returns:
            The data as a ``pyarrow.Table``.

        Raises:
            FileNotFoundError: If nothing exists at the resolved path.
        """
        filesystem, path = fs.filesystem_for(self.path)
        if filesystem.get_file_info(path).type == pafs.FileType.NotFound:
            raise FileNotFoundError(f"Parquet file not found: {self.path}")
        df = parquet.read_table(path, filesystem=filesystem)
        logger.info("Read %d rows from Parquet %s", df.num_rows, self.path)
        return df

    def put(self, df: pa.Table, allow_empty: bool = False) -> int:
        """Write ``df`` to a single Parquet file, replacing it atomically.

        Args:
            df: Data to write.
            allow_empty: Write a file with the schema and no rows when ``df`` is empty.

        Returns:
            The number of rows written.

        Raises:
            ValueError: If ``df`` is ``None``, or empty and ``allow_empty`` is false.
        """
        df = self._check_input(df, allow_empty=allow_empty)
        self._atomic_write(lambda where: parquet.write_table(df, where))
        logger.info("Wrote %d rows to Parquet %s", df.num_rows, self.path)
        return df.num_rows


__all__ = ["Parquet"]
