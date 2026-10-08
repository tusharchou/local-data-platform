"""CSV files read and written with ``pyarrow.csv``, on local disk or an object store."""

import os

import pyarrow as pa
from pyarrow import csv
from pyarrow import fs as pafs

from local_data_platform import fs
from local_data_platform.exceptions import ConfigError
from local_data_platform.format import Format
from local_data_platform.logger import get_logger

logger = get_logger(__name__)


class CSV(Format):
    """A CSV file on the local filesystem or an object store.

    Args:
        name: Dataset name.
        path: File path or URI. Absolute paths are kept; relative paths resolve against
            ``base_dir`` (or the cwd). See :func:`local_data_platform.paths.resolve_path`.
            ``file://``, ``s3://`` and ``gs://`` URIs are accepted; see :mod:`local_data_platform.fs`.
        base_dir: Folder that relative paths resolve against, usually the config file's folder.
        **options: Extra options kept on ``self.options``.

    Attributes:
        path: The resolved location: an absolute ``Path`` for local files, the URI string for
            object stores.
    """

    def __init__(self, name: str, path: str | os.PathLike, base_dir: str | os.PathLike | None = None, **options):
        if not path:
            raise ConfigError(f"CSV dataset {name!r} needs a path")
        super().__init__(name, path=None, base_dir=base_dir, **options)
        self._set_location(path, base_dir)

    def get(self) -> pa.Table:
        """Read the whole file. A ``.gz``, ``.bz2`` or ``.zst`` suffix is decompressed.

        Returns:
            The file contents as a ``pyarrow.Table``.

        Raises:
            FileNotFoundError: If no file exists at the resolved path.
        """
        filesystem, path = fs.filesystem_for(self.path)
        if filesystem.get_file_info(path).type != pafs.FileType.File:
            raise FileNotFoundError(f"CSV file not found: {self.path}")
        with filesystem.open_input_stream(path) as stream:
            df = csv.read_csv(stream)
        logger.info("Read %d rows from CSV %s", df.num_rows, self.path)
        return df

    def put(self, df: pa.Table, allow_empty: bool = False) -> int:
        """Write ``df`` to the file, replacing it atomically.

        Args:
            df: Data to write.
            allow_empty: Write a header-only file when ``df`` has no rows.

        Returns:
            The number of rows written.

        Raises:
            ValueError: If ``df`` is ``None``, or empty and ``allow_empty`` is false.
        """
        df = self._check_input(df, allow_empty=allow_empty)
        self._atomic_write(lambda where: csv.write_csv(df, where))
        logger.info("Wrote %d rows to CSV %s", df.num_rows, self.path)
        return df.num_rows


__all__ = ["CSV"]
