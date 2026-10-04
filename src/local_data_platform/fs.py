"""Filesystem access for local paths and object stores, through ``pyarrow.fs``.

Contract: ``docs/design/v0_2_0.md`` section C2. Every file format (CSV, Parquet, JSON) opens its
data through this module, so a dataset path can be any of:

* a local path, absolute or relative. It follows :func:`local_data_platform.paths.resolve_path`
  (relative paths resolve against ``base_dir``, the config file's folder, or the cwd);
* a ``file://`` URI (``file:///abs/path``, or Hadoop's ``file:/abs/path``);
* an ``s3://`` URI (``s3a://`` and ``s3n://`` are accepted as aliases), read with
  :class:`pyarrow.fs.S3FileSystem`;
* a ``gs://`` URI (alias ``gcs://``), read with :class:`pyarrow.fs.GcsFileSystem`.

Object-store settings come from the environment when a filesystem is created, never from a
config file:

* S3 credentials come from the AWS SDK's default chain (``AWS_ACCESS_KEY_ID`` and
  ``AWS_SECRET_ACCESS_KEY``, ``AWS_PROFILE``, instance metadata and so on). This module never
  reads, stores or logs them.
* ``AWS_ENDPOINT_URL_S3``, else ``AWS_ENDPOINT_URL``, points S3 at another endpoint such as MinIO,
  LocalStack or a moto server (``http://127.0.0.1:9000``). pyarrow does not read these variables
  itself, so this module does.
* ``AWS_REGION``, else ``AWS_DEFAULT_REGION``, sets the S3 region.
* ``STORAGE_EMULATOR_HOST`` points GCS at an emulator (anonymous access). Otherwise GCS uses
  Google's default credentials; ``GOOGLE_CLOUD_PROJECT`` sets the project.

Credentials embedded in a URI (``s3://key:secret@bucket/...``) are rejected, so a secret cannot
end up in a config file or a log line.

Writes are atomic: readers see the old object or the new one, never a partial write.

* Locally, data goes to a temporary file in the destination folder, which then replaces the
  target with one ``os.replace``.
* On object stores, data is buffered in memory and uploaded only once the writer has finished
  without error. The upload is a single PUT for objects below pyarrow's multipart threshold, and
  an S3 PUT is atomic per object; a larger object is a multipart upload, which becomes visible
  only when it completes. If the writer raises, nothing is uploaded.
"""

import os
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pyarrow as pa
from pyarrow import fs as pafs

from .exceptions import ConfigError, EngineNotFound
from .logger import get_logger
from .paths import resolve_path

logger = get_logger(__name__)

PathOrUri = str | os.PathLike

S3_SCHEMES = ("s3", "s3a", "s3n")
"""URI schemes read with :class:`pyarrow.fs.S3FileSystem`."""

GCS_SCHEMES = ("gs", "gcs")
"""URI schemes read with :class:`pyarrow.fs.GcsFileSystem`."""

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")

WriteCallback = Callable[[str | pa.NativeFile], None]
"""A writer for :func:`write_atomic`: it gets a local temporary path, or a writable stream."""


def uri_scheme(uri: PathOrUri) -> str | None:
    """Return the lower-case scheme of ``uri`` (``"s3"``, ``"file"``, ...), or ``None`` for a plain path."""
    text = os.fsdecode(os.fspath(uri))
    match = _SCHEME.match(text)
    if match:
        return match.group(1).lower()
    if text[:5].lower() == "file:":
        return "file"
    return None


def is_remote(uri: PathOrUri) -> bool:
    """Return whether ``uri`` names an object store (``s3://``, ``gs://``) rather than a local path."""
    scheme = uri_scheme(uri)
    return scheme is not None and scheme != "file"


def _check_scheme(scheme: str, uri: str) -> None:
    if scheme not in S3_SCHEMES + GCS_SCHEMES:
        raise ConfigError(f"unsupported URI scheme {scheme!r} in {uri!r}; use a local path, file://, s3:// or gs://")


def _split_remote(uri: str) -> tuple[str, str, str]:
    """Split an object-store URI into ``(scheme, bucket, key)``, validating it."""
    scheme = uri_scheme(uri) or ""
    _check_scheme(scheme, uri)
    rest = uri[len(scheme) + 3:]
    bucket, _, key = rest.partition("/")
    if "@" in bucket:
        raise ConfigError(f"{scheme}:// URI {_redact_userinfo(uri)!r} embeds credentials; set them in "
                          "environment variables instead")
    if not bucket:
        raise ConfigError(f"{scheme}:// URI {uri!r} has no bucket")
    return scheme, bucket, key.rstrip("/")


def _redact_userinfo(uri: str) -> str:
    scheme = uri_scheme(uri) or ""
    rest = uri[len(scheme) + 3:]
    bucket, sep, key = rest.partition("/")
    return f"{scheme}://***@{bucket.rpartition('@')[2]}{sep}{key}"


def _local_path(uri: PathOrUri, base_dir: PathOrUri | None) -> Path:
    text = os.fsdecode(os.fspath(uri))
    if uri_scheme(text) == "file":
        parts = urlsplit(text)
        if parts.netloc not in ("", "localhost"):
            raise ConfigError(f"file:// URI {text!r} names host {parts.netloc!r}; only local files are supported")
        text = unquote(parts.path)
        if not text:
            raise ConfigError(f"file:// URI {uri!r} has no path")
    return resolve_path(text, base_dir)


def resolve_location(uri: PathOrUri, base_dir: PathOrUri | None = None) -> Path | str:
    """Resolve a dataset location: a local ``Path``, or a normalised object-store URI string.

    Args:
        uri: A local path, ``file://`` URI, ``s3://`` URI or ``gs://`` URI.
        base_dir: Folder that relative local paths resolve against (default: the cwd).

    Returns:
        An absolute ``Path`` for local locations, following
        :func:`local_data_platform.paths.resolve_path`; the URI (without a trailing slash) for
        object stores.

    Raises:
        ValueError: If ``uri`` is empty.
        ConfigError: If the scheme is unsupported, the bucket is missing, or the URI embeds credentials.
    """
    if uri is None or os.fspath(uri) in ("", b""):
        raise ValueError("path must be a non-empty string or PathLike")
    if is_remote(uri):
        text = os.fsdecode(os.fspath(uri))
        scheme, bucket, key = _split_remote(text)
        return f"{scheme}://{bucket}/{key}" if key else f"{scheme}://{bucket}"
    return _local_path(uri, base_dir)


def _env(*names: str, environ: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if environ is None else environ
    for name in names:
        value = source.get(name)
        if value:
            return value
    return None


def _endpoint(url: str) -> tuple[str | None, str]:
    """Split ``http://host:port`` into ``("http", "host:port")``; a bare ``host:port`` has no scheme."""
    if "://" not in url:
        return None, url.rstrip("/")
    parts = urlsplit(url)
    return parts.scheme.lower() or None, (parts.netloc + parts.path).rstrip("/")


def s3_options(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The non-secret :class:`pyarrow.fs.S3FileSystem` options this module takes from the environment.

    Args:
        environ: The environment to read (default ``os.environ``).

    Returns:
        A dict with any of ``endpoint_override``, ``scheme`` and ``region``. Credentials are
        never included: the AWS SDK reads them itself.
    """
    options: dict[str, str] = {}
    endpoint = _env("AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL", environ=environ)
    if endpoint:
        scheme, override = _endpoint(endpoint)
        options["endpoint_override"] = override
        if scheme:
            options["scheme"] = scheme
    region = _env("AWS_REGION", "AWS_DEFAULT_REGION", environ=environ)
    if region:
        options["region"] = region
    return options


def gcs_options(environ: Mapping[str, str] | None = None) -> dict[str, str | bool]:
    """The :class:`pyarrow.fs.GcsFileSystem` options this module takes from the environment.

    Args:
        environ: The environment to read (default ``os.environ``).

    Returns:
        ``endpoint_override``, ``scheme`` and ``anonymous=True`` when ``STORAGE_EMULATOR_HOST`` is
        set, and ``project_id`` when ``GOOGLE_CLOUD_PROJECT`` is set.
    """
    options: dict[str, str | bool] = {}
    emulator = _env("STORAGE_EMULATOR_HOST", environ=environ)
    if emulator:
        scheme, override = _endpoint(emulator)
        options.update(endpoint_override=override, scheme=scheme or "http", anonymous=True)
    project = _env("GOOGLE_CLOUD_PROJECT", environ=environ)
    if project:
        options["project_id"] = project
    return options


def _s3_filesystem() -> pafs.FileSystem:
    options = s3_options()
    logger.debug("Creating S3FileSystem with %s", options or "the AWS SDK defaults")
    return pafs.S3FileSystem(**options)


def _gcs_filesystem() -> pafs.FileSystem:
    try:
        from pyarrow.fs import GcsFileSystem
    except ImportError as exc:  # pyarrow built without GCS support
        raise EngineNotFound("gs:// paths need a pyarrow build with GCS support; install the wheel from PyPI: "
                             "pip install --force-reinstall pyarrow") from exc
    options = gcs_options()
    logger.debug("Creating GcsFileSystem with %s", options or "Google's default credentials")
    return GcsFileSystem(**options)


def filesystem_for(uri: PathOrUri, base_dir: PathOrUri | None = None) -> tuple[pafs.FileSystem, str]:
    """Return the pyarrow filesystem for ``uri`` and the path to use with it.

    Object-store filesystems are created on every call, so they pick up the current environment.

    Args:
        uri: A local path, ``file://`` URI, ``s3://`` URI or ``gs://`` URI.
        base_dir: Folder that relative local paths resolve against (default: the cwd).

    Returns:
        ``(filesystem, path)``: a :class:`pyarrow.fs.LocalFileSystem` and an absolute path, or an
        object-store filesystem and ``"bucket/key"``.

    Raises:
        ConfigError: If the URI is invalid or its scheme is unsupported.
        EngineNotFound: If pyarrow lacks the filesystem the scheme needs.
    """
    location = resolve_location(uri, base_dir)
    if isinstance(location, Path):
        return pafs.LocalFileSystem(), str(location)
    scheme, bucket, key = _split_remote(location)
    path = f"{bucket}/{key}" if key else bucket
    if scheme in S3_SCHEMES:
        return _s3_filesystem(), path
    return _gcs_filesystem(), path


def _info(uri: PathOrUri, base_dir: PathOrUri | None) -> pafs.FileInfo:
    filesystem, path = filesystem_for(uri, base_dir)
    return filesystem.get_file_info(path)


def exists(uri: PathOrUri, base_dir: PathOrUri | None = None) -> bool:
    """Return whether a file or folder (an object or a key prefix on object stores) exists at ``uri``."""
    return _info(uri, base_dir).type != pafs.FileType.NotFound


def is_file(uri: PathOrUri, base_dir: PathOrUri | None = None) -> bool:
    """Return whether ``uri`` is a file (an object, on object stores)."""
    return _info(uri, base_dir).type == pafs.FileType.File


def open_input(uri: PathOrUri, base_dir: PathOrUri | None = None) -> pa.NativeFile:
    """Open ``uri`` for random-access reading, as Parquet readers need. The caller closes it.

    Raises:
        FileNotFoundError: If nothing exists at ``uri``.
    """
    filesystem, path = filesystem_for(uri, base_dir)
    return filesystem.open_input_file(path)


def open_input_stream(uri: PathOrUri, base_dir: PathOrUri | None = None) -> pa.NativeFile:
    """Open ``uri`` for sequential reading, decompressing it by extension (``.gz``, ``.bz2``, ``.zst``...).

    The caller closes it.

    Raises:
        FileNotFoundError: If nothing exists at ``uri``.
    """
    filesystem, path = filesystem_for(uri, base_dir)
    return filesystem.open_input_stream(path)


def _temporary_sibling(target: Path) -> Path:
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")


def _upload(filesystem: pafs.FileSystem, path: str, data: pa.Buffer) -> None:
    with filesystem.open_output_stream(path) as stream:
        stream.write(data)
    logger.debug("Uploaded %d bytes to %s", data.size, path)


@contextmanager
def open_output_atomic(uri: PathOrUri, base_dir: PathOrUri | None = None) -> Iterator[pa.NativeFile]:
    """Open ``uri`` for writing; the data becomes visible only when the ``with`` block succeeds.

    Locally, the stream writes a temporary file next to the target (parent folders are created),
    which replaces the target on success and is removed on error. On object stores, the stream is
    an in-memory buffer that is uploaded on success and discarded on error.

    Args:
        uri: The file to write: a local path, ``file://``, ``s3://`` or ``gs://`` URI.
        base_dir: Folder that relative local paths resolve against.

    Yields:
        A writable ``pyarrow.NativeFile``. Closing it inside the block is allowed.
    """
    filesystem, path = filesystem_for(uri, base_dir)
    if isinstance(filesystem, pafs.LocalFileSystem):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = _temporary_sibling(target)
        stream = pa.OSFile(str(tmp), "wb")
        try:
            yield stream
            stream.close()
            os.replace(tmp, target)
        except BaseException:
            stream.close()
            tmp.unlink(missing_ok=True)
            raise
        return
    buffer = pa.BufferOutputStream()
    yield buffer
    _upload(filesystem, path, buffer.getvalue())


def write_atomic(uri: PathOrUri, write: WriteCallback, base_dir: PathOrUri | None = None) -> None:
    """Write ``uri`` atomically with ``write``, which does the actual serialisation.

    Locally, ``write`` gets the path (a ``str``) of a temporary file in the destination folder,
    which replaces the target in one ``os.replace`` once ``write`` returns. On object stores,
    ``write`` gets an in-memory stream that is uploaded once ``write`` returns. pyarrow writers
    such as ``pyarrow.csv.write_csv`` and ``pyarrow.parquet.write_table`` accept either.

    If ``write`` raises, the temporary file is removed, nothing is uploaded and the existing
    file or object is left untouched.

    Args:
        uri: The file to write.
        write: A callable that writes the full output to the path or stream it is given.
        base_dir: Folder that relative local paths resolve against.
    """
    filesystem, path = filesystem_for(uri, base_dir)
    if isinstance(filesystem, pafs.LocalFileSystem):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = _temporary_sibling(target)
        try:
            write(str(tmp))
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return
    buffer = pa.BufferOutputStream()
    write(buffer)
    _upload(filesystem, path, buffer.getvalue())


__all__ = [
    "GCS_SCHEMES",
    "S3_SCHEMES",
    "exists",
    "filesystem_for",
    "gcs_options",
    "is_file",
    "is_remote",
    "open_input",
    "open_input_stream",
    "open_output_atomic",
    "resolve_location",
    "s3_options",
    "uri_scheme",
    "write_atomic",
]
