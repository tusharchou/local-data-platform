"""CSV files read and written with ``pyarrow.csv``, on local disk or an object store, and read over https."""

import math
import os

import pyarrow as pa
from pyarrow import csv
from pyarrow import fs as pafs

from local_data_platform import fs, urls
from local_data_platform.exceptions import ConfigError
from local_data_platform.format import Format
from local_data_platform.logger import get_logger

logger = get_logger(__name__)


class CSV(Format):
    """A CSV file on the local filesystem or an object store, or one that is read over https.

    Args:
        name: Dataset name.
        path: File path or URI. Absolute paths are kept; relative paths resolve against
            ``base_dir`` (or the cwd). See :func:`local_data_platform.paths.resolve_path`.
            ``file://``, ``s3://`` and ``gs://`` URIs are accepted; see :mod:`local_data_platform.fs`.
            An ``https://`` address is read, and cannot be written, see :mod:`local_data_platform.urls`.
        base_dir: Folder that relative paths resolve against, usually the config file's folder.
        timeout: For an ``https://`` path, the seconds that the whole download may take.
            :data:`local_data_platform.urls.DEFAULT_TIMEOUT_S` by default.
        max_bytes: For an ``https://`` path, the most bytes that the download may have after it is decompressed.
            :data:`local_data_platform.urls.DEFAULT_MAX_BYTES` by default.
        **options: Extra options kept on ``self.options``.

    Attributes:
        path: The resolved location: an absolute ``Path`` for local files, the URI string for
            object stores, and the address for https.
        timeout: The seconds of the download, for an ``https://`` path, else ``None``.
        max_bytes: The most bytes of the download, for an ``https://`` path, else ``None``.

    Raises:
        ConfigError: If there is no path, the address is not allowed, ``timeout`` or ``max_bytes`` is not a number above
            0, or one of them is given for a path that is not an ``https://`` address.
    """

    def __init__(self, name: str, path: str | os.PathLike, base_dir: str | os.PathLike | None = None, *,
                 timeout: float | None = None, max_bytes: int | None = None, **options):
        if not path:
            raise ConfigError(f"CSV dataset {name!r} needs a path")
        super().__init__(name, path=None, base_dir=base_dir, **options)
        self.timeout = self.max_bytes = None
        if urls.is_http(path):
            self._set_address(path, timeout, max_bytes)
        elif timeout is not None or max_bytes is not None:
            raise ConfigError(f"CSV dataset {name!r} has timeout or max_bytes, which are for an https:// path")
        else:
            self._set_location(path, base_dir)

    def _set_address(self, address: str, timeout: float | None, max_bytes: int | None) -> None:
        reason = urls.problem(address)
        if reason:
            raise ConfigError(f"CSV dataset {self.name!r} cannot read {urls.label(address)}, "
                              f"because the address {reason}")
        valid_timeout = timeout is None or (isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                                            and math.isfinite(timeout) and timeout > 0)
        if not valid_timeout:
            raise ConfigError(f"CSV dataset {self.name!r} needs a timeout that is a number of seconds above 0, "
                              f"got {timeout!r}")
        valid_size = max_bytes is None or (isinstance(max_bytes, int) and not isinstance(max_bytes, bool)
                                           and max_bytes > 0)
        if not valid_size:
            raise ConfigError(f"CSV dataset {self.name!r} needs a max_bytes that is a whole number of bytes above 0, "
                              f"got {max_bytes!r}")
        self.path = address
        self.timeout = urls.DEFAULT_TIMEOUT_S if timeout is None else timeout
        self.max_bytes = urls.DEFAULT_MAX_BYTES if max_bytes is None else max_bytes

    def __repr__(self) -> str:
        if isinstance(self.path, str) and urls.is_http(self.path):  # no query, which may hold a secret
            return f"{type(self).__name__}(name={self.name!r}, path={urls.label(self.path)!r})"
        return super().__repr__()

    def get(self) -> pa.Table:
        """Read the whole file. A ``.gz``, ``.bz2`` or ``.zst`` suffix is decompressed.

        An ``https://`` path is downloaded as :func:`local_data_platform.urls.fetch` says, and is read as plain CSV.

        Returns:
            The file contents as a ``pyarrow.Table``.

        Raises:
            FileNotFoundError: If no file exists at the resolved path.
            ConfigError: For an ``https://`` path, if the download is refused, as
                :func:`~local_data_platform.urls.fetch` says.
            OSError: For an ``https://`` path, if the host cannot be reached, or the download is too slow.
        """
        if urls.is_http(self.path):
            df = csv.read_csv(pa.BufferReader(urls.fetch(self.path, timeout=self.timeout, max_bytes=self.max_bytes)))
            logger.info("Read %d rows from CSV %s", df.num_rows, urls.label(self.path))
            return df
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
            ConfigError: If the path is an ``https://`` address, which is read and not written.
        """
        if urls.is_http(self.path):
            raise ConfigError(f"CSV dataset {self.name!r} reads {urls.label(self.path)}, which cannot be written")
        df = self._check_input(df, allow_empty=allow_empty)
        self._atomic_write(lambda where: csv.write_csv(df, where))
        logger.info("Wrote %d rows to CSV %s", df.num_rows, self.path)
        return df.num_rows


__all__ = ["CSV"]
