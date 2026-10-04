"""File and table formats.

Concrete formats live in sub-packages (``format.csv``, ``format.parquet``,
``format.iceberg``) so that importing one never pulls in the dependencies of
another.

File formats read and write through :mod:`local_data_platform.fs`, so their path can be
a local path or an object-store URI (``s3://``, ``gs://``).
"""

import os
from collections.abc import Callable

import pyarrow as pa

from local_data_platform import Table
from local_data_platform import fs


class Format(Table):
    """Base class for the dataset formats (CSV, Parquet, Iceberg)."""

    def _set_location(self, path: str | os.PathLike, base_dir: str | os.PathLike | None) -> None:
        """Set ``self.path`` from a local path or an object-store URI.

        Local paths become an absolute ``Path`` (see :func:`local_data_platform.paths.resolve_path`);
        ``s3://`` and ``gs://`` URIs are kept as strings. See :func:`local_data_platform.fs.resolve_location`.
        """
        self.path = fs.resolve_location(path, base_dir)

    @staticmethod
    def _check_input(df: pa.Table | pa.RecordBatch | None, allow_empty: bool = False) -> pa.Table:
        """Validate data handed to ``put`` and return it as a ``pyarrow.Table``.

        Args:
            df: The data to write.
            allow_empty: Accept a table with zero rows.

        Returns:
            The data as a ``pyarrow.Table``.

        Raises:
            ValueError: If ``df`` is ``None``, or has no rows and ``allow_empty`` is false.
            TypeError: If ``df`` is not a ``pyarrow.Table`` or ``pyarrow.RecordBatch``.
        """
        if df is None:
            raise ValueError("No data to write: got None instead of a pyarrow.Table")
        if isinstance(df, pa.RecordBatch):
            df = pa.Table.from_batches([df])
        if not isinstance(df, pa.Table):
            raise TypeError(f"Expected a pyarrow.Table, got {type(df).__name__}")
        if df.num_rows == 0 and not allow_empty:
            raise ValueError("No data to write: the table has 0 rows (pass allow_empty=True to write it anyway)")
        return df

    def _atomic_write(self, write: Callable[[str | pa.NativeFile], None]) -> None:
        """Write ``self.path`` atomically with :func:`local_data_platform.fs.write_atomic`.

        Locally, ``write`` receives a temporary path in the destination folder. Once it
        returns, the temporary file replaces ``self.path`` in one ``os.replace`` call, so
        readers see either the old file or the new one, never a partial write. Parent
        folders are created as needed. If ``write`` fails, the temporary file is removed
        and the existing file is left untouched.

        On an object store, ``write`` receives an in-memory stream that is uploaded (one
        PUT) only after ``write`` returns, so a failed write uploads nothing.

        Args:
            write: A callable that writes the full output to the path or stream it is given.
        """
        fs.write_atomic(self.path, write)


__all__ = ["Format"]
