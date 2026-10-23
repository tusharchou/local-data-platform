"""Gold tables as files that people open without Python, Iceberg or an engineer.

:func:`export_table` writes one gold table as a CSV, Parquet, JSONL or Excel file, and :func:`write_duckdb_file` writes
the whole gold layer as one DuckDB file, with the tables, what each table and column means, a ``_catalog`` table and a
``_metrics`` table. Both read the gold layer the way every reader of it does. By default that is the current release,
``release`` names an older one, and ``live`` reads the tables as they are now. A DuckDB file holds the tables of one
release and nothing else, so the file is as consistent as the release is.

``ldp gold export`` and ``ldp gold duckdb`` are these two functions, and ``ldp gold build --duckdb`` calls
:func:`refresh_after_build`. Every file is written to a temporary path next to its final one and renamed, so a reader
never sees a half-written file, and a failed write leaves the file that was there.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import decimal
import json
import math
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_data_platform import datasets
from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.format.excel import INSTALL_HINT as EXCEL_HINT
from local_data_platform.gold import catalog as catalogs
from local_data_platform.gold import release as releases
from local_data_platform.gold.build import GoldConfig, find_gold
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path

if TYPE_CHECKING:
    import pyarrow as pa

    from local_data_platform.gold.build import BuildResult
    from local_data_platform.gold.catalog import CatalogEntry
    from local_data_platform.gold.release import Release

logger = get_logger(__name__)

EXPORT_FORMATS = ("csv", "parquet", "jsonl", "xlsx")
"""The formats :func:`export_table` writes."""

EXTENSIONS = {".csv": "csv", ".parquet": "parquet", ".jsonl": "jsonl", ".xlsx": "xlsx"}
"""The format that the end of a file name says."""

XLSX_ROWS = 1_048_576
"""The rows of an Excel sheet, with the header row."""

XLSX_COLUMNS = 16_384
"""The columns of an Excel sheet."""

XLSX_CELL_CHARS = 32_767
"""The characters that an Excel cell holds."""

_SHEET_NAME_CHARS = 31
_EXACT_INTEGERS = 2 ** 53
_BATCH_ROWS = 10_000

CATALOG_TABLE = "_catalog"
"""The table of a DuckDB file that says what each table is."""

METRICS_TABLE = "_metrics"
"""The table of a DuckDB file that holds the metric definitions."""

STORAGE_VERSION = "v1.0.0"
"""The storage version that a DuckDB file is written with. DuckDB 2.0 writes a newer one by default, which a DuckDB 1.x
cannot open, and every DuckDB from 1.0 on reads this one."""

DUCKDB_HINT = 'pip install "local-data-platform[duckdb]"'

_CATALOG_COLUMNS = {
    "table_name": "The table, as it is called in this file.",
    "description": "What the table means.",
    "owner": "Who owns it.",
    "grain": "What one row is.",
    "release": "The release of the gold layer that the table was copied from, or no value for the tables as they were "
               "when the file was written.",
    "snapshot_id": "The snapshot of the Iceberg table that was copied.",
    "built_at": "When that snapshot was written, in UTC, which is when the table was built.",
}
_METRICS_COLUMNS = {
    "metric": "The name of the metric.",
    "description": "What it means.",
    "owner": "Who answers for it.",
    "type": "simple for one measure, ratio for one measure over another.",
    "format": "How a person reads it, number, percent or currency.",
    "table_name": "The table that it is computed from.",
    "definition": "The SQL that computes it from that table, for the rows that are asked about.",
    "dimensions": "The columns that it can be grouped and filtered by, separated by commas.",
    "time_column": "The date column of its table, or no value.",
    "grains": "The grains of time that it may be reported at, separated by commas.",
}


# --------------------------------------------------------------------------- what a call did


@dataclass(frozen=True)
class Exported:
    """What :func:`export_table` wrote.

    Attributes:
        table: The gold table, ``<namespace>.<table>``.
        dest: Where it was written.
        format: ``csv``, ``parquet``, ``jsonl`` or ``xlsx``.
        rows: The rows written.
        columns: The columns written.
        release: The release that the table was read as, or ``None`` when it was read as it is now.
        snapshot_id: The snapshot that was read, or ``None`` for a table that has no snapshot yet.
        source: Which version of the table was read, in words.
    """

    table: str
    dest: str
    format: str
    rows: int
    columns: int
    release: str | None
    snapshot_id: int | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"table": self.table, "dest": self.dest, "format": self.format, "rows": self.rows,
                "columns": self.columns, "release": self.release, "snapshot_id": self.snapshot_id,
                "source": self.source}

    def summary(self) -> str:
        """One line that says what was written, such as ``wrote x.csv, 12 rows of gold.sales, from release r3``."""
        rows = f"{self.rows} row{'' if self.rows == 1 else 's'}"
        snapshot = f", snapshot {self.snapshot_id}" if self.snapshot_id is not None else ""
        return f"wrote {self.dest}, {rows} of {self.table}, read from {self.source}{snapshot}"


@dataclass(frozen=True)
class DuckDBFile:
    """What :func:`write_duckdb_file` wrote.

    Attributes:
        path: The file.
        release: The release that the tables were copied from, or ``None`` for the tables as they were.
        tables: The tables in the file, as they are called there.
        rows: The rows of every table.
        metrics: The metrics in the ``_metrics`` table.
        left_out: The gold tables that the release does not hold, which the file does not have.
        source: Which version of the tables was read, in words.
        size: The size of the file in bytes.
    """

    path: Path
    release: str | None
    tables: tuple[str, ...]
    rows: int
    metrics: int
    left_out: tuple[str, ...]
    source: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"path": str(self.path), "release": self.release, "tables": list(self.tables), "rows": self.rows,
                "metrics": self.metrics, "left_out": list(self.left_out), "source": self.source, "size": self.size}

    def summary(self) -> str:
        """A sentence that says what the file holds."""
        tables = f"{len(self.tables)} table{'' if len(self.tables) == 1 else 's'}"
        metrics = f" and {self.metrics} metric{'' if self.metrics == 1 else 's'}" if self.metrics else ""
        text = f"wrote {self.path}, {tables}{metrics}, from {self.source}, {self.size / 1024:.1f} KB"
        if self.left_out:
            text += f". Left out, because {self.release} does not hold them, {', '.join(self.left_out)}"
        return text


@dataclass(frozen=True)
class FileOutcome:
    """What ``ldp gold build --duckdb`` did about the file.

    Attributes:
        path: The file, with ``{release}`` in its name replaced when there is a release.
        status: ``written``, ``kept`` when it already held the current release, ``left`` when the build did not end
            well and the file was not touched, or ``failed`` when it could not be written.
        reason: Why, for ``kept`` and ``left``.
        file: What was written, for ``written``.
        error: What went wrong, for ``failed``.
    """

    path: Path
    status: str
    reason: str = ""
    file: DuckDBFile | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The outcome as JSON-serialisable primitives."""
        return {"path": str(self.path), "status": self.status, "reason": self.reason or None,
                "file": self.file.to_dict() if self.file is not None else None, "error": self.error}

    def summary(self) -> str:
        """One line for the output of the build."""
        if self.status == "written" and self.file is not None:
            return self.file.summary()
        if self.status == "failed":
            return f"{self.path} was not written"
        return f"{self.path} was left as it is, because {self.reason}"


# --------------------------------------------------------------------------- reading one version of the tables


@dataclass(frozen=True)
class _Version:
    """The gold tables of a folder, and the release that a read of them uses."""

    gold: list[GoldConfig]
    chosen: Release | None
    named: bool
    live: bool


def _version(folder: str | os.PathLike, release: str | None, live: bool) -> _Version:
    found = find_gold(folder)
    if not found:
        raise ConfigError(f"{resolve_path(folder)} has no gold config. A gold config has metadata.docs.layer set to "
                          "gold")
    return _Version(found, releases.choose_release(folder, release=release, live=live), release is not None, live)


def _entries(folder: str | os.PathLike, only: str | None = None) -> Mapping[str, CatalogEntry]:
    """What the catalog says about the gold tables of a folder, or about one of them, by table."""
    return {entry.table: entry for entry in catalogs.collect(folder, only=only).entries}


def _describe(version: _Version, table: str) -> str:
    from local_data_platform.metrics.runner import describe_source

    held = version.chosen is not None and releases.snapshot_for(table, version.chosen) is not None
    return describe_source(version.chosen, held, table, version.live)


def _moment(timestamp_ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=dt.timezone.utc)


@dataclass(frozen=True)
class _Read:
    rows: pa.Table
    snapshot_id: int | None
    built_at: dt.datetime | None
    rows_in_snapshot: int


def _read(gold: GoldConfig, snapshot: int | None, check: Callable[[int], None] | None = None) -> _Read:
    """The rows of a gold table at a snapshot, or at its current one when ``snapshot`` is ``None``.

    ``check`` is called with the number of rows before they are read, and may raise to stop the read.
    """
    from local_data_platform.pipeline.builders import iceberg_from_config

    iceberg = iceberg_from_config(gold.config, "target", must_exist=True)
    loaded = iceberg.table()
    if snapshot is None:
        current = loaded.current_snapshot()
        snapshot = current.snapshot_id if current is not None else None
    elif loaded.snapshot_by_id(snapshot) is None:
        raise releases.ReleaseError(f"the release names snapshot {snapshot} of {gold.table}, which no longer exists, "
                                    f"so the table cannot be read as of that release")
    found = loaded.snapshot_by_id(snapshot) if snapshot is not None else None
    if check is not None:
        try:
            counted = int((loaded.scan(snapshot_id=snapshot) if snapshot is not None else loaded.scan()).count())
        except Exception:  # noqa: BLE001 - the check only saves a read, and a writer checks the rows it is given
            counted = None
        if counted is not None:
            check(counted)
    data = iceberg.get(snapshot_id=snapshot)
    return _Read(data, snapshot, _moment(found.timestamp_ms) if found is not None else None, data.num_rows)


def _snapshot_of(version: _Version, table: str) -> int | None:
    """The snapshot a read of ``table`` uses, which is ``None`` for the current one.

    Raises:
        ConfigError: If a release was asked for by name and it does not hold the table.
    """
    snapshot = releases.snapshot_for(table, version.chosen)
    if version.named and snapshot is None:
        raise ConfigError(f"release {version.chosen.name} does not hold {table}, so it cannot be read as of that "
                          f"release. The release holds {sorted(version.chosen.snapshots())}")
    return snapshot


# --------------------------------------------------------------------------- one table as a file


def _format_of(dest: str, format: str | None) -> str:
    suffix = Path(dest).suffix.lower()
    named = EXTENSIONS.get(suffix)
    if format is None:
        if named is None:
            raise ConfigError(f"cannot tell the format of {dest} from its name. End the name with one of "
                              f"{sorted(EXTENSIONS)}, or choose one with --format {{{','.join(EXPORT_FORMATS)}}}")
        return named
    chosen = str(format).strip().lower()
    if chosen not in EXPORT_FORMATS:
        raise ConfigError(f"unknown export format {format!r}. The formats are {list(EXPORT_FORMATS)}")
    if named is not None and named != chosen:
        raise ConfigError(f"{dest} ends in {suffix}, which is {named}, and the format is {chosen}. Change one of them")
    return chosen


def _plain(value: str | None) -> str:
    return value if value else ""


_TYPE_WORDS = (("is_integer", "whole number"), ("is_floating", "number"), ("is_decimal", "decimal number"),
               ("is_boolean", "true or false"), ("is_date", "date"), ("is_time", "time"))


def _type_name(kind: Any) -> str:
    """What a column holds, in words for a person, such as ``whole number`` or ``date and time (UTC)``."""
    import pyarrow as pa

    for check, words in _TYPE_WORDS:
        if getattr(pa.types, check)(kind):
            return words
    if pa.types.is_timestamp(kind):
        return "date and time (UTC)" if kind.tz else "date and time"
    if pa.types.is_string(kind) or pa.types.is_large_string(kind):
        return "text"
    if pa.types.is_nested(kind):
        return "a list or a record, written as JSON"
    return str(kind)


def _about(entry: CatalogEntry | None, table: str, read: _Read, release: Release | None, exported_at: dt.datetime
           ) -> list[tuple[str, Any]]:
    stamp = "%Y-%m-%d %H:%M:%S"
    rows = [("Table", table), ("Description", _plain(entry.description if entry else None)),
            ("Owner", _plain(entry.owner if entry else None)), ("Grain", _plain(entry.grain if entry else None)),
            ("Primary key", ", ".join(entry.primary_key) if entry else ""),
            ("Release", release.name if release is not None else "none, the table as it was when the file was written"),
            ("Snapshot id", "" if read.snapshot_id is None else read.snapshot_id),
            ("Built at (UTC)", read.built_at.strftime(stamp) if read.built_at is not None else ""),
            ("Rows", read.rows_in_snapshot), ("Exported at (UTC)", exported_at.strftime(stamp))]
    return rows


def _import_openpyxl() -> Any:
    try:
        import openpyxl
    except ImportError as error:
        raise EngineNotFound(f"An XLSX export needs the openpyxl package. Install it with: {EXCEL_HINT}") from error
    return openpyxl


def _sheet_name(table: str) -> str:
    name = re.sub(r"[\[\]:*?/\\]", "_", table.rpartition(".")[2])[:_SHEET_NAME_CHARS] or "table"
    return f"{name[:_SHEET_NAME_CHARS - 5]}_data" if name.lower() == "about" else name


def _cell(sheet: Any, value: Any, column: str) -> Any:
    """The value of a spreadsheet cell for a value of a table. A text is always a text, and never a formula."""
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    if value is None:
        return None
    if isinstance(value, dt.datetime):  # before date, which it is a kind of. A sheet has no time zones
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, dt.date):
        return value
    if isinstance(value, dt.time):
        return value.replace(tzinfo=None)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):  # a bool is one too, and is a number to a sheet
        if abs(value) <= _EXACT_INTEGERS:
            return value
        value = str(value)  # a sheet holds numbers as doubles, which round an integer this large
    elif isinstance(value, (bytes, bytearray)):
        value = bytes(value).hex()
    elif isinstance(value, (list, dict, tuple)):
        value = json.dumps(value, default=str, ensure_ascii=False)
    elif not isinstance(value, str):
        value = str(value)
    cleaned = ILLEGAL_CHARACTERS_RE.sub("", value)
    if len(cleaned) > XLSX_CELL_CHARS:
        raise ConfigError(f"a value in the column {column!r} has {len(cleaned)} characters, and an Excel cell holds "
                          f"{XLSX_CELL_CHARS}. Export CSV or Parquet instead")
    cell = WriteOnlyCell(sheet, value=cleaned)
    cell.data_type = "s"
    return cell


def _write_xlsx(path: Path, data: pa.Table, table: str, about: Sequence[tuple[str, Any]],
                columns: Sequence[tuple[str, str, str]]) -> None:
    """Write ``data`` as the first sheet of a workbook and ``about`` as the second.

    Raises:
        ConfigError: If the rows or the columns do not fit a sheet, or a value does not fit a cell.
        EngineNotFound: If openpyxl is not installed.
    """
    openpyxl = _import_openpyxl()
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    if data.num_rows + 1 > XLSX_ROWS:
        raise ConfigError(f"{table} has {data.num_rows} rows, and an Excel sheet holds {XLSX_ROWS - 1} below its "
                          "header. Export CSV or Parquet instead")
    if data.num_columns > XLSX_COLUMNS:
        raise ConfigError(f"{table} has {data.num_columns} columns, and an Excel sheet holds {XLSX_COLUMNS}. "
                          "Export CSV or Parquet instead")
    workbook = openpyxl.Workbook(write_only=True)
    try:
        sheet = workbook.create_sheet(_sheet_name(table))
        sheet.freeze_panes = "A2"
        bold = Font(bold=True)
        names = data.schema.names
        for index, name in enumerate(names, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = min(max(len(name) + 2, 10), 40)
        if names:
            sheet.auto_filter.ref = f"A1:{get_column_letter(len(names))}{max(data.num_rows, 1) + 1}"
        header = []
        for name in names:
            cell = WriteOnlyCell(sheet, value=name)
            cell.font = bold
            cell.data_type = "s"
            header.append(cell)
        sheet.append(header)
        for batch in data.to_batches(max_chunksize=_BATCH_ROWS):
            pieces = [batch.column(index).to_pylist() for index in range(batch.num_columns)]
            for row in zip(*pieces) if pieces else ():
                sheet.append([_cell(sheet, value, name) for value, name in zip(row, names)])
        notes = workbook.create_sheet("about")
        notes.column_dimensions["A"].width = 18
        notes.column_dimensions["B"].width = 60
        notes.column_dimensions["C"].width = 70
        for label, value in about:
            label_cell = WriteOnlyCell(notes, value=label)
            label_cell.font = bold
            notes.append([label_cell, _cell(notes, value, label) if value != "" else None])
        notes.append([])
        notes.append([_bold(notes, text, bold) for text in ("Column", "Type", "Description")])
        for name, kind, description in columns:
            notes.append([_cell(notes, name, "Column"), kind, _cell(notes, description, name) if description else None])
        workbook.save(path)
    except BaseException:
        _abandon(workbook)
        raise


def _abandon(workbook: Any) -> None:
    """Close a workbook that will not be saved, with each sheet closed before the files that it writes to.

    A sheet of a write-only workbook is a generator that holds a file open. If the file is closed first, the generator
    writes its closing tag to a closed file when it is collected, and Python prints that as an ignored exception.
    """
    for sheet in workbook.worksheets:
        rows = getattr(sheet, "_rows", None)
        if rows is not None:
            try:
                rows.close()
            except Exception:  # noqa: BLE001 - the workbook is being thrown away
                pass
    workbook.close()


def _bold(sheet: Any, text: str, font: Any) -> Any:
    from openpyxl.cell import WriteOnlyCell

    cell = WriteOnlyCell(sheet, value=text)
    cell.font = font
    return cell


def _temporary(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")


def _replace(temporary: Path, path: Path) -> None:
    """Move a finished file over the one that readers see. It never leaves half a file at ``path``."""
    try:
        os.replace(temporary, path)
    except PermissionError as error:
        raise ConfigError(f"{path} cannot be replaced, because another program has it open, which Windows does not "
                          f"allow. Close it, or write a file with a new name for each release, such as "
                          f"{path.with_name(path.stem + '_{release}' + path.suffix)}") from error


def _fits_a_sheet(table: str) -> Callable[[int], None]:
    """A check that a table of that many rows fits the sheet of a workbook, for :func:`_read`."""

    def check(counted: int) -> None:
        if counted + 1 > XLSX_ROWS:
            raise ConfigError(f"{table} has {counted} rows, and an Excel sheet holds {XLSX_ROWS - 1} below its "
                              "header. Export CSV or Parquet instead")

    return check


def export_table(folder: str | os.PathLike, table: str, dest: str | os.PathLike, *, format: str | None = None,
                 release: str | None = None, live: bool = False, base_dir: str | os.PathLike | None = None
                 ) -> Exported:
    """Write one gold table as a file.

    The table is read the way every reader of the gold layer reads it. That is the current release by default, so a
    file and a report that were made after the same build agree. ``release`` reads an older one, and ``live`` reads the
    table as it is now. A table that the current release does not hold, and a folder that has no release yet, are read
    as the table is now, and the result says so.

    Args:
        folder: A folder of configs with gold tables.
        table: The table, as ``<namespace>.<table>`` or by its name when only one gold table has it.
        dest: The file. A relative path resolves against ``base_dir``, else the working folder. Parquet, JSONL and CSV
            may be an ``s3://`` or ``gs://`` URI, and XLSX is written to a local path.
        format: ``csv``, ``parquet``, ``jsonl`` or ``xlsx``. It comes from the end of ``dest`` when it is not given.
        release: Read this release, such as ``r3``, and not the current one.
        live: Read the table as it is now.
        base_dir: The folder that a relative ``dest`` resolves against.

    Returns:
        What was written.

    Raises:
        ConfigError: If the format cannot be told or is not known, the table is not a gold table of the folder,
            ``release`` does not hold it, or an Excel sheet cannot hold it.
        ReleaseError: If ``release`` is not a release of the folder, or a snapshot of it is gone.
        TableNotFound: If the gold table does not exist yet.
        EngineNotFound: If the format is ``xlsx`` and openpyxl is not installed.
    """
    text = os.fspath(dest)
    fmt = _format_of(text, format)
    version = _version(folder, release, live)
    identifier = catalogs.resolve_table([gold.table for gold in version.gold], table, str(folder))
    gold = next(item for item in version.gold if item.table == identifier)
    snapshot = _snapshot_of(version, identifier)
    exported_at = dt.datetime.now(dt.timezone.utc)
    check = None
    if fmt == "xlsx":
        if "://" in text and not text.startswith("file://"):
            raise ConfigError(f"an XLSX file is written to a local path, and {text} is not one. Export CSV or "
                              "Parquet to a URI")
        _import_openpyxl()
        check = _fits_a_sheet(identifier)
    read = _read(gold, snapshot, check)
    used = version.chosen if releases.snapshot_for(identifier, version.chosen) is not None else None
    rows = read.rows
    if fmt == "xlsx":
        entry = _entries(folder, only=identifier).get(identifier)
        described = {item.name: item.description for item in entry.columns if item.description} if entry else {}
        columns = [(field.name, _type_name(field.type), described.get(field.name) or "") for field in rows.schema]
        path = resolve_path(text.removeprefix("file://"), base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary(path)
        try:
            _write_xlsx(temporary, rows, identifier, _about(entry, identifier, read, used, exported_at), columns)
            _replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    else:
        if fmt == "parquet":
            metadata = dict(rows.schema.metadata or {})
            metadata[b"ldp.gold"] = json.dumps({"table": identifier, "release": used.name if used else None,
                                                "snapshot_id": read.snapshot_id}, sort_keys=True).encode()
            rows = rows.replace_schema_metadata(metadata)
        datasets.write_rows(rows, text, fmt, base_dir=base_dir)
    logger.info("Exported %s (%d rows) to %s as %s", identifier, rows.num_rows, text, fmt)
    return Exported(table=identifier, dest=text, format=fmt, rows=rows.num_rows, columns=rows.num_columns,
                    release=used.name if used else None, snapshot_id=read.snapshot_id,
                    source=_describe(version, identifier))


# --------------------------------------------------------------------------- the DuckDB file


def _import_duckdb() -> Any:
    try:
        import duckdb
    except ImportError as error:
        raise EngineNotFound(f"The DuckDB file of the gold layer needs the duckdb package. Install it with: "
                             f"{DUCKDB_HINT}") from error
    return duckdb


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def expand(path: str | os.PathLike, release: str | None) -> Path:
    """A path with ``{release}`` in its name replaced by the release, or by ``live`` when there is none.

    A DuckDB file that a program holds open cannot be replaced on Windows, so a name with ``{release}`` in it is a new
    file for each release.
    """
    return Path(os.fspath(path).replace("{release}", release or "live"))


def _definition(definitions: Any, metric: Any) -> str:
    from local_data_platform.metrics.sql import aggregate, ratio

    model = definitions.model_of(metric)

    def one(name: str) -> str:
        measure = model.measure(name)
        return aggregate(measure.agg, measure.expr) if measure is not None else name

    if metric.type == "simple":
        return one(metric.measure)
    return ratio(one(metric.numerator), one(metric.denominator))


def _metric_rows(folder: str | os.PathLike, names: Mapping[str, str]) -> list[dict[str, Any]]:
    """The rows of ``_metrics``, for the metrics of the tables that are in the file. ``names`` maps a gold table to its
    name in the file.

    Raises:
        ConfigError: If a metrics file is not valid.
    """
    from local_data_platform.metrics.load import NO_FILES, read_definitions
    from local_data_platform.metrics.load import resolve_table as resolve_gold

    found, problems = read_definitions(folder)
    if found is None:
        if problems and not problems[0].startswith(NO_FILES):
            raise ConfigError(f"the metrics of {resolve_path(folder)} are not valid, so they cannot go into the file. "
                              + " ".join(problems))
        return []
    rows = []
    for model in found.models:
        try:
            table = resolve_gold(model.table, list(names))
        except ConfigError:
            continue
        for metric in found.metrics_of(model):
            info = found.describe(metric)
            rows.append({"metric": metric.name, "description": metric.description, "owner": metric.owner,
                         "type": metric.type, "format": metric.format, "table_name": names[table],
                         "definition": _definition(found, metric), "dimensions": ", ".join(info["dimensions"]),
                         "time_column": info["time_column"], "grains": ", ".join(info["grains"])})
    return rows


def _attach(connection: Any, duckdb: Any, path: Path) -> None:
    """Attach the file that is being written, in a storage version that every DuckDB from 1.0 on opens."""
    target = _literal(str(path))
    try:
        connection.execute(f"ATTACH {target} AS ldp_out (STORAGE_VERSION {_literal(STORAGE_VERSION)})")
    except duckdb.Error:
        connection.execute(f"ATTACH {target} AS ldp_out")  # a DuckDB that has no such option writes its own version


def _remove(temporary: Path) -> None:
    """Remove a file that was being written, and the write-ahead log that DuckDB may have left next to it."""
    for leftover in (temporary, temporary.with_name(temporary.name + ".wal")):
        leftover.unlink(missing_ok=True)


def _create(connection: Any, name: str, data: pa.Table, comment: str | None, columns: Mapping[str, str]) -> None:
    connection.register("_ldp_rows", data)
    try:
        connection.execute(f"CREATE TABLE ldp_out.main.{_quote(name)} AS SELECT * FROM _ldp_rows")
    finally:
        connection.unregister("_ldp_rows")
    if comment:
        connection.execute(f"COMMENT ON TABLE ldp_out.main.{_quote(name)} IS {_literal(comment)}")
    for column, text in columns.items():
        if column in data.schema.names:  # the contract may describe a column that an older snapshot does not have
            connection.execute(f"COMMENT ON COLUMN ldp_out.main.{_quote(name)}.{_quote(column)} IS {_literal(text)}")


def _catalog_table(rows: Iterable[Mapping[str, Any]]) -> pa.Table:
    import pyarrow as pa

    rows = list(rows)
    schema = pa.schema([("table_name", pa.string()), ("description", pa.string()), ("owner", pa.string()),
                        ("grain", pa.string()), ("release", pa.string()), ("snapshot_id", pa.int64()),
                        ("built_at", pa.timestamp("us"))])
    return pa.table({field.name: pa.array([row[field.name] for row in rows], field.type) for field in schema},
                    schema=schema)


def _metrics_table(rows: Iterable[Mapping[str, Any]]) -> pa.Table:
    import pyarrow as pa

    rows = list(rows)
    schema = pa.schema([(name, pa.string()) for name in _METRICS_COLUMNS])
    return pa.table({field.name: pa.array([row[field.name] for row in rows], field.type) for field in schema},
                    schema=schema)


def write_duckdb_file(folder: str | os.PathLike, out: str | os.PathLike, *, release: str | None = None,
                      live: bool = False) -> DuckDBFile:
    """Write the gold layer of a folder as one DuckDB file.

    The file holds a copy of every gold table of the release, a ``COMMENT ON TABLE`` and a ``COMMENT ON COLUMN`` from
    the contract of each, a ``_catalog`` table with one row for each table, and a ``_metrics`` table with the metric
    definitions. The tables are in the ``main`` schema under their own names, so that ``SELECT * FROM daily_sales``
    works, and no schema has the name of the file, which DuckDB would find ambiguous. The file needs no DuckDB
    extension to open, and it is written in a storage version that DuckDB 1.x opens too.

    It is written to a temporary path next to ``out`` and renamed, so a reader never sees a half-written file, and a
    failure leaves the file that was there. The release is the current one by default, ``release`` names another, and
    ``live`` copies the tables as they are now. A gold table that the release does not hold is left out and named in
    the result, so that the file is as consistent as the release is.

    Args:
        folder: A folder of configs with gold tables, and a metrics file if it has one.
        out: The file. ``{release}`` in its name is replaced by the name of the release, or by ``live``.
        release: Copy this release, such as ``r3``, and not the current one.
        live: Copy the tables as they are now.

    Returns:
        What was written.

    Raises:
        ConfigError: If the folder has no gold table, a gold table is called ``_catalog`` or ``_metrics``, or a metrics
            file is not valid.
        ReleaseError: If ``release`` is not a release of the folder, or a snapshot of it is gone.
        TableNotFound: If a gold table does not exist yet.
        EngineNotFound: If duckdb is not installed.
    """
    duckdb = _import_duckdb()
    version = _version(folder, release, live)
    chosen = version.chosen
    path = expand(out, chosen.name if chosen is not None else None)
    names = {gold.table: str(gold.config.metadata["target"]["name"]) for gold in version.gold}
    clash = sorted(name for name in names.values() if name in (CATALOG_TABLE, METRICS_TABLE))
    if clash:
        raise ConfigError(f"a gold table is called {clash[0]}, which the DuckDB file uses for its own table. Rename "
                          "the table")
    wanted = [gold for gold in version.gold if chosen is None or releases.snapshot_for(gold.table, chosen) is not None]
    held = {gold.table for gold in wanted}
    left_out = tuple(sorted(gold.table for gold in version.gold if gold.table not in held))
    metric_rows = _metric_rows(folder, {gold.table: names[gold.table] for gold in wanted})
    entries = _entries(folder)
    catalog_rows: list[dict[str, Any]] = []
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(path)
    rows = 0
    try:
        with contextlib.closing(duckdb.connect()) as connection:
            _attach(connection, duckdb, temporary)
            for gold in sorted(wanted, key=lambda item: item.table):
                entry = entries.get(gold.table)
                read = _read(gold, releases.snapshot_for(gold.table, chosen))
                docs = {item.name: item.description for item in entry.columns if item.description} if entry else {}
                name = names[gold.table]
                _create(connection, name, read.rows, entry.description if entry else None, docs)
                rows += read.rows.num_rows
                catalog_rows.append({"table_name": name, "description": entry.description if entry else None,
                                     "owner": entry.owner if entry else None, "grain": entry.grain if entry else None,
                                     "release": chosen.name if chosen is not None else None,
                                     "snapshot_id": read.snapshot_id,
                                     "built_at": read.built_at.replace(tzinfo=None) if read.built_at else None})
            _create(connection, CATALOG_TABLE, _catalog_table(catalog_rows),
                    "One row for each gold table in this file, with what it means, who owns it and where it came "
                    "from.", _CATALOG_COLUMNS)
            _create(connection, METRICS_TABLE, _metrics_table(metric_rows),
                    "The metrics that are defined on the tables of this file.", _METRICS_COLUMNS)
            connection.execute("DETACH ldp_out")
        _replace(temporary, path)
    except BaseException:
        _remove(temporary)
        raise
    tables = tuple(row["table_name"] for row in catalog_rows)
    source = _source_text(version)
    logger.info("Wrote %s with %d tables from %s", path, len(tables), source)
    return DuckDBFile(path=path, release=chosen.name if chosen is not None else None, tables=tables, rows=rows,
                      metrics=len(metric_rows), left_out=left_out, source=source, size=path.stat().st_size)


def _source_text(version: _Version) -> str:
    from local_data_platform.metrics.runner import describe_source

    if version.live:
        return describe_source(None, False, "", True)
    if version.chosen is None:
        return describe_source(None, False, "")
    return describe_source(version.chosen, True, "")


def file_release(path: str | os.PathLike) -> str | None:
    """The release that a DuckDB file of the gold layer holds, or ``None`` when it holds none or cannot be read.

    Nothing is written, and a file that is not there, or is not one of these, gives ``None``.
    """
    try:
        duckdb = _import_duckdb()
        with contextlib.closing(duckdb.connect(str(path), read_only=True)) as connection:
            found = [row[0] for row in connection.execute(f"SELECT DISTINCT release FROM {CATALOG_TABLE}").fetchall()]
    except Exception:  # noqa: BLE001 - a file that cannot be read is a file that has to be written again
        return None
    return found[0] if len(found) == 1 else None


def refresh_after_build(folder: str | os.PathLike, out: str | os.PathLike, result: BuildResult) -> FileOutcome:
    """Write the DuckDB file for the release that a build ended with, when the build went well.

    The file is written when the build cut a release. When the build cut none because the tables are at the snapshots
    of the current release already, the file is written only if it is missing or holds another release. A build that
    was blocked or failed does not touch the file, so the file readers have stays the one of the last good release.

    Args:
        folder: The folder that was built.
        out: The file. ``{release}`` in its name is replaced by the release.
        result: What ``build_gold`` returned.

    Returns:
        What was done. A file that could not be written is a ``failed`` outcome, and does not raise.
    """
    outcome = result.release
    release = outcome.release if outcome is not None else None
    if result.exit_code != 0 or outcome is None or release is None or outcome.error:
        reason = "the build did not end well" if result.exit_code != 0 or (outcome and outcome.error) \
            else "there is no release"
        return FileOutcome(expand(out, release.name if release is not None else None), "left", reason)
    path = expand(out, release.name)
    if not outcome.cut and file_release(path) == release.name:
        return FileOutcome(path, "kept", f"it already holds release {release.name}")
    try:
        written = write_duckdb_file(folder, out, release=release.name)
    except Exception as error:  # noqa: BLE001 - the release is cut and the tables are built, so it is reported
        logger.warning("Could not write %s: %s", path, error)
        return FileOutcome(path, "failed", error=f"{type(error).__name__}: {error}")
    return FileOutcome(path, "written", file=written)


__all__ = [
    "CATALOG_TABLE",
    "DuckDBFile",
    "EXPORT_FORMATS",
    "EXTENSIONS",
    "Exported",
    "FileOutcome",
    "METRICS_TABLE",
    "STORAGE_VERSION",
    "XLSX_CELL_CHARS",
    "XLSX_COLUMNS",
    "XLSX_ROWS",
    "expand",
    "export_table",
    "file_release",
    "refresh_after_build",
    "write_duckdb_file",
]
