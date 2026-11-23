"""A report as JSON, for an agent or a program that wants the numbers and not the page.

The document has the report, the window and the time zone, the version of each table that was read, the rows of every
query and what each block shows. A number is a JSON number, as it is in ``ldp metrics query --json``, and a date is
text. Nothing is rounded or scaled by a format, and a block says its values both as they were computed and as the page
writes them. A value that is not a finite number, such as ``nan``, is ``null`` in the numbers and the text that the
page writes in the texts. It is the same run that the page is made from, so the two agree.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from local_data_platform.report.formats import utc_text
from local_data_platform.report.view import Chart, Grid, Kpi, Words

if TYPE_CHECKING:
    from local_data_platform.report.engine import QueryRun, ReportRun

DOCUMENT_VERSION = 1
"""The version of the layout of the document, written as ``ldp_report``."""


def _instant(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _value(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_text))


def _text(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):  # a number, and a whole one when it has no scale
        exponent = value.as_tuple().exponent
        if not isinstance(exponent, int):  # nan and the infinities, which JSON cannot write
            return None
        return int(value) if exponent >= 0 else float(value)
    return str(value)


def _clean(value: Any) -> Any:
    """The value with every float that JSON cannot write, ``nan`` and the infinities, replaced by ``None``."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def _query(item: QueryRun) -> dict[str, Any]:
    from local_data_platform.metrics.render import json_rows

    info = {column["name"]: column for column in item.columns_info}
    columns = []
    for field in item.table.schema:
        extra = info.get(field.name, {})
        columns.append({"name": field.name, "type": str(field.type), "kind": extra.get("kind"),
                        "format": extra.get("format"), "description": extra.get("description")})
    return {"kind": item.kind, "rows": item.rows, "truncated": item.truncated, "columns": columns,
            "data": json_rows(item.table)}


def _view(view: Any) -> dict[str, Any]:
    block = view.block
    base = {"index": block.index, "type": block.type, "title": getattr(view, "title", None), "notes": []}
    if isinstance(view, Kpi):
        return {**base, "result": view.result, "value": view.value, "raw": _value(view.raw), "compare": view.compare,
                "raw_compare": _value(view.raw_compare), "change": view.change, "direction": view.direction,
                "format": view.format, "notes": list(view.notes)}
    if isinstance(view, Chart):
        return {**base, "result": view.result, "x": view.x, "y": view.y, "series": view.series_column,
                "format": view.format, "categories": list(view.categories), "rows": view.rows,
                "total_rows": view.total_rows, "notes": list(view.notes),
                "values": [{"name": item.name, "values": list(item.values), "texts": list(item.texts)}
                           for item in view.series]}
    if isinstance(view, Grid):
        return {**base, "result": view.result, "columns": list(view.headers), "rows": len(view.rows),
                "total_rows": view.total_rows, "notes": list(view.notes), "cells": [list(row) for row in view.rows]}
    if isinstance(view, Words):
        return {**base, "paragraphs": list(view.paragraphs)}
    raise TypeError(f"unknown view {type(view).__name__}")


def document(run: ReportRun, *, built_at: dt.datetime | None = None) -> dict[str, Any]:
    """The document of a run as a dict that ``json.dumps`` writes.

    Args:
        run: A run whose blocks were resolved.
        built_at: The moment of the build. The clock by default.
    """
    report = run.report
    moment = built_at if built_at is not None else dt.datetime.now(dt.timezone.utc)
    return {
        "ldp_report": DOCUMENT_VERSION,
        "report": {"identifier": report.identifier, "title": report.title, "who": report.who, "what": report.what,
                   "where": report.where, "when": report.when, "how": report.how},
        "window": {"start": _instant(run.window[0]), "end": _instant(run.window[1])} if run.window else None,
        "timezone": report.timezone, "built_at": _instant(moment), "built": utc_text(moment),
        "release": run.release, "live": run.live,
        "parameters": {name: _value(value) for name, value in run.parameters.items()},
        "tables": [read.to_dict() for read in run.reads.values()],
        "health": run.health.to_dict() if run.health is not None else None,
        "queries": {name: _query(item) for name, item in run.queries.items() if item.table is not None},
        "blocks": [_view(view) for view in run.views],
    }


def render_json(run: ReportRun, *, built_at: dt.datetime | None = None) -> str:
    """The document of a run as indented JSON text that ends with a newline."""
    return json.dumps(_clean(document(run, built_at=built_at)), indent=2, ensure_ascii=False, allow_nan=False) + "\n"


__all__ = ["DOCUMENT_VERSION", "document", "render_json"]
