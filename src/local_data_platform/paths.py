"""Path resolution for dataset configs.

Rules, in order:

1. Absolute paths that exist are used as they are.
2. Relative paths resolve against ``base_dir`` (the config file's folder), or the
   current working directory when no ``base_dir`` is given.
3. Legacy configs (before 0.1.1) wrote cwd-relative paths with a leading slash, such
   as ``"/rides.csv"``. If an absolute path doesn't exist but the same path without
   the leading slash exists under ``base_dir`` or the cwd, that path is used and a
   ``DeprecationWarning`` is emitted.
4. The same fallback applies to an output that doesn't exist yet (a CSV target such as
   ``"/out/rides.csv"``, or a new ``"/warehouse"``): when the absolute path's parent
   folder doesn't exist, or is the filesystem root, but the relative parent folder exists
   under ``base_dir`` or the cwd, the relative path is used, with the same warning.
5. Otherwise the absolute path is returned unchanged, so callers can create it.

The warning is attributed to the first caller outside this package, so Python's default
filters show it when that caller is a script.
"""

import os
import warnings
from pathlib import Path

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep


def _warn_legacy(path: str | os.PathLike, root: Path, what: str = "it") -> None:
    warnings.warn(
        f"Path {str(path)!r} is written as absolute but {what} only exists relative to {root}. "
        "Write it without the leading slash; legacy support will be removed in 0.2.0.",
        DeprecationWarning,
        skip_file_prefixes=(_PACKAGE_DIR,),
    )


def resolve_path(path: str | os.PathLike, base_dir: str | os.PathLike | None = None) -> Path:
    """Resolve ``path`` following the module rules and return an absolute ``Path``."""
    if path is None or str(path) == "":
        raise ValueError("path must be a non-empty string or PathLike")
    raw = Path(os.path.expanduser(str(path)))
    base = Path(base_dir).expanduser().resolve() if base_dir else Path.cwd()

    if not raw.is_absolute():
        return (base / raw).resolve()

    if raw.exists():
        return raw

    stripped = str(raw).lstrip("/\\")
    if not stripped:
        return raw
    roots = list(dict.fromkeys([base, Path.cwd()]))
    for root in roots:
        candidate = (root / stripped).resolve()
        if candidate.exists():
            _warn_legacy(path, root)
            return candidate

    # A new output: keep a real absolute path whose parent folder exists, otherwise fall back
    # to the legacy relative form when its parent folder exists.
    if raw.parent.exists() and raw.parent != Path(raw.anchor):
        return raw
    for root in roots:
        candidate = (root / stripped).resolve()
        if candidate.parent.is_dir():
            _warn_legacy(path, root, "its folder")
            return candidate
    return raw
