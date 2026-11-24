"""A report that goes out on a schedule: the ``report`` task, ``metadata.after`` and ``ldp report send --window``.

The project is a small shop. A pipeline loads the drop file of each day into ``gold.gold_sales``, and a report of the
orders of the window goes to a Slack channel and to a page. Every post goes to the local server of
``tests/conftest.py``, so none leaves the machine, and no test waits for a retry. Every test names the time of its tick
with ``now``, so nothing depends on the clock of the day it runs.

``@daily`` fires at midnight UTC and the fire of day N owns day N - 1, so a tick at 02:05 on day 3 runs day 2.
"""

import datetime as dt
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from local_data_platform.cli import main  # noqa: E402
from local_data_platform.orchestration import schedule as schedule_module  # noqa: E402

START = dt.date(2026, 8, 1)
UTC = dt.timezone.utc
SECRET = "XXSECRETXXTOKEN"
CATALOG = {"identifier": "gold", "warehouse_path": "../warehouse"}
QUERIES = {"totals": {"sql": "SELECT COALESCE(sum(orders), 0) AS orders FROM gold.gold_sales "
                             "WHERE placed_at >= $window_start AND placed_at < $window_end"}}
BLOCKS = [{"type": "kpi", "title": "Orders", "query": "totals", "value": "orders", "format": "integer"}]
PAGE = "https://example.com/reports/daily.html"
README = Path(__file__).resolve().parents[1] / "README.md"


# ====================================================================== helpers


def _day(n: int) -> dt.date:
    """Day number ``n``, from 1: 2026-08-01 is day 1."""
    return START + dt.timedelta(days=n - 1)


def _span(n: int) -> str:
    """The window of day ``n``, as ``START/END`` in UTC."""
    return f"{_day(n).isoformat()}T00:00:00Z/{_day(n + 1).isoformat()}T00:00:00Z"


def _at(n: int, clock: str = "02:05") -> str:
    """A time on day ``n``, as ISO text in UTC."""
    return f"{_day(n).isoformat()}T{clock}:00Z"


def _write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def _drop(root: Path, n: int, rows: int = 2) -> Path:
    """The drop file of day ``n``: ``rows`` orders of 5 each, at 10:00, 11:00, ..."""
    day = _day(n).isoformat()
    lines = ["placed_at,city,orders"] + [f"{day}T{10 + i:02d}:00:00,Pune,5" for i in range(rows)]
    return _write(root / "landing" / f"orders_{day}.csv", "\n".join(lines) + "\n")


def _pipeline(root: Path, *, observability: bool | list = True) -> Path:
    """The config that loads the drop file of a window into ``gold.gold_sales``, with a sink for its runs.

    ``observability`` is ``False`` for no sink, ``True`` for its events file, or the list of sinks to use.
    """
    metadata: dict = {
        "source": {"name": "orders", "format": "CSV", "path": "../landing/orders_{window_start:%Y-%m-%d}.csv"},
        "target": {"name": "gold_sales", "format": "ICEBERG", "write_mode": "append", "catalog": CATALOG},
        "window": {"size": "day", "column": "placed_at"},
        "docs": {"layer": "gold", "description": "The orders of each day.", "grain": "one row per order",
                 "columns": {"placed_at": "When the order was placed.", "city": "Where it was placed.",
                             "orders": "How many orders."}}}
    if observability:
        metadata["observability"] = {"sinks": ["jsonl"] if observability is True else observability}
    return _write(root / "config" / "gold_sales.json", {"identifier": "gold_sales", "who": "ops", "metadata": metadata})


def _report(root: Path, *, delivery=None, after=None, freshness=None, observability=True, timezone=None,
            name: str = "daily.json") -> Path:
    """The report of the orders of a window, which goes to a Slack channel and to a page by default."""
    metadata: dict = {"title": "Daily orders", "tables": {"config": ["../config"], "allow": ["gold.*"]},
                      "queries": dict(QUERIES), "blocks": list(BLOCKS)}
    metadata["delivery"] = delivery if delivery is not None else {
        "team": {"type": "slack", "webhook_env": "SLACK_URL"},
        "page": {"type": "file", "path": "out/daily.html", "public_url": PAGE}}
    if after is not None:
        metadata["after"] = after
    if freshness is not None:
        metadata["freshness"] = freshness
    if observability:
        metadata["observability"] = {"sinks": [{"type": "jsonl", "path": "events.jsonl"}]}
    if timezone is not None:
        metadata["timezone"] = timezone
    return _write(root / "reports" / name, {"apiVersion": "ldp/v1", "kind": "Report", "identifier": "daily",
                                            "what": "The orders of the window.", "metadata": metadata})


def _workflow(root: Path, name: str, identifier: str, tasks: list, *, schedule: str | None = "@daily",
              size: str | None = "day", timezone: str | None = None, extra: dict | None = None) -> Path:
    metadata: dict = {}
    if schedule:
        metadata["schedule"] = schedule
    if size:
        metadata["window"] = {"size": size, **({"timezone": timezone} if timezone else {})}
    metadata.update(extra or {})
    return _write(root / name, {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": identifier,
                                "metadata": metadata, "tasks": tasks})


LOAD = {"id": "sales", "pipeline": "config/gold_sales.json"}
SEND = {"id": "daily", "report": {"spec": "reports/daily.json", "send": True}}


def _ingest(root: Path, *, name: str = "ingest.json", **keywords) -> Path:
    """A workflow that loads the day, and nothing else."""
    return _workflow(root, name, "ingest", [LOAD], **keywords)


def _own(root: Path, *, name: str = "send_daily.json", send: bool = True, **keywords) -> Path:
    """A report on its own schedule: a workflow of one report task."""
    return _workflow(root, name, "daily_report",
                     [{"id": "daily", "report": {"spec": "reports/daily.json", "send": send}}], **keywords)


@pytest.fixture
def hooks(lineage_server, monkeypatch):
    """``SLACK_URL`` points at the local server, with a path that looks like a secret. Yields its handler."""
    url, handler = lineage_server
    monkeypatch.setenv("SLACK_URL", f"{url}/slack/{SECRET}")
    handler.status = 200
    return handler


@pytest.fixture
def shop(tmp_path, hooks):
    """Five drop files, the pipeline that loads them and the report, and nothing scheduled yet."""
    for n in range(1, 6):
        _drop(tmp_path, n)
    _pipeline(tmp_path)
    _report(tmp_path)
    return tmp_path


def _tick(root: Path, n: int, clock: str = "02:05", **options):
    return schedule_module.tick(root, now=_at(n, clock), sleep=lambda seconds: None, **options)


def _posts(hooks) -> int:
    return len(hooks.received)


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


def _report_events(root: Path) -> list[dict]:
    return _lines(root / "reports" / "events.jsonl")


def _ledger(root: Path) -> list[dict]:
    return _lines(root / ".ldp" / "events.jsonl")


def _types(events: list[dict]) -> list[str]:
    return [item["type"] for item in events]


def _statuses(result) -> list[tuple[str, str]]:
    """``(window, outcome)`` of each window that a tick looked at, over every workflow."""
    return [(entry.window, entry.outcome) for item in result.workflows if item.result for entry in item.result.windows]


# ====================================================================== a report on its own schedule


def test_a_report_on_its_own_schedule_is_sent_for_the_window_that_the_fire_owns(shop, hooks):
    _own(shop)
    _ingest(shop)

    first = _tick(shop, 3)

    assert first.exit_code == 0
    delivered = [item for item in _report_events(shop) if item["type"] == "report.delivered"]
    assert sorted(item["payload"]["destination"] for item in delivered) == ["page", "team"]
    assert {item["payload"]["window"] for item in delivered} == {_span(2)}
    assert _posts(hooks) == 1
    assert (shop / "reports" / "out" / "daily.html").is_file()


def test_a_report_on_its_own_schedule_is_sent_once_and_a_second_tick_in_the_window_sends_nothing(shop, hooks):
    _own(shop)
    _ingest(shop)
    _tick(shop, 3, "02:05")
    assert _posts(hooks) == 1

    again = _tick(shop, 3, "02:10")

    assert again.exit_code == 0 and again.counts() == {"published": 0, "skipped": 2, "blocked": 0, "failed": 0}
    assert _posts(hooks) == 1
    assert _types(_report_events(shop)).count("report.delivered") == 2


def test_the_next_day_is_the_next_window_and_the_next_delivery(shop, hooks):
    _own(shop)
    _ingest(shop)

    _tick(shop, 3)
    second = _tick(shop, 4)

    assert second.exit_code == 0 and _posts(hooks) == 2
    windows = [item["payload"]["window"] for item in _report_events(shop) if item["type"] == "report.delivered"]
    assert sorted(set(windows)) == [_span(2), _span(3)]
    assert (shop / "reports" / "out" / "daily.html").is_file()


# ====================================================================== the pipelines that a report waits for


def _bounds(n: int) -> tuple[dt.datetime, dt.datetime]:
    midnight = dt.time()
    return (dt.datetime.combine(_day(n), midnight, tzinfo=UTC), dt.datetime.combine(_day(n + 1), midnight, tzinfo=UTC))


def _record(root: Path, number: int, window: int | None, status: str, *, name: str = "gold_sales", attempts: int = 1,
            span: tuple[str, str] | None = None) -> None:
    """One run of a pipeline in the file of its jsonl sink: ``window`` is the day number, or ``None`` for no window.

    ``status`` is how it ended, ``published``, ``skipped_duplicate``, ``blocked_quality``, ``failed``, or ``running``
    for a run that has no terminal event. ``span`` is the window as the run wrote it, when it is not a day.
    """
    from local_data_platform.events import RunEvent

    run_id = f"run-{name}-{number}"
    moment = dt.datetime(2026, 8, 10, 1, number, tzinfo=UTC)
    shown = list(span) if span else ([b.isoformat() for b in _bounds(window)] if window else None)
    base = {"pipeline": name, "table": f"gold.{name}"}
    lines = []
    for attempt in range(1, attempts + 1):
        events = [("run.started", {"mode": "append", "logical_window": shown})]
        last = attempt == attempts
        if last and status != "running":
            terminal = {"published": "run.published", "skipped_duplicate": "run.skipped_duplicate",
                        "blocked_quality": "run.blocked_quality", "failed": "run.failed"}[status]
            events += [(terminal, {"rows_written": 2}), ("run.finished", {"status": status, "duration_s": 1.0})]
        elif not last:
            events += [("run.failed", {"error_type": "OSError"}), ("run.finished", {"status": "failed"})]
        for seq, (kind, payload) in enumerate(events):
            lines.append(RunEvent(event_id=f"{run_id}-{attempt}-{seq}", type=kind, schema_version=1,
                                  ts=moment + dt.timedelta(seconds=seq), run_id=run_id, attempt=attempt, seq=seq,
                                  payload={**base, **payload}).to_json())
    folder = root / "config" / ".ldp"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.writelines(line + "\n" for line in lines)


def _waiting(root: Path, n: int = 2, **report):
    from local_data_platform.report.after import waiting_for
    from local_data_platform.report.spec import load_report

    path = _report(root, **report)
    return waiting_for(load_report(path), _bounds(n))


def test_a_report_that_waits_for_no_pipeline_waits_for_nothing_and_reads_nothing(shop):
    from local_data_platform.report.after import waiting_for
    from local_data_platform.report.spec import load_report

    assert waiting_for(load_report(_report(shop)), _bounds(2)) == []
    assert not (shop / "config" / ".ldp").exists()


@pytest.mark.parametrize("status", ["published", "skipped_duplicate"])
def test_a_pipeline_that_published_the_window_or_found_it_published_is_not_waited_for(shop, status):
    _record(shop, 1, 2, status)

    assert _waiting(shop, after=["gold_sales"]) == []


def test_a_pipeline_that_has_not_run_for_the_window_is_waited_for_and_the_reason_says_so(shop):
    _record(shop, 1, 3, "published")  # another day

    waiting = _waiting(shop, after=["gold_sales"])

    assert [(item.pipeline, item.reason) for item in waiting] == [("gold_sales", "it has not run for the window")]


@pytest.mark.parametrize("status", ["failed", "blocked_quality"])
def test_a_run_of_the_window_that_did_not_publish_is_waited_for_and_the_reason_names_how_it_ended(shop, status):
    _record(shop, 1, 2, status)

    waiting = _waiting(shop, after=["gold_sales"])

    assert [item.reason for item in waiting] == [f"its last run for the window ended {status}"]


def test_a_run_that_has_not_ended_is_waited_for_as_still_running(shop):
    _record(shop, 1, 2, "running")

    assert [item.reason for item in _waiting(shop, after=["gold_sales"])] == ["it is still running for the window"]


def test_a_run_that_was_retried_and_published_on_the_last_attempt_counts(shop):
    _record(shop, 1, 2, "published", attempts=3)

    assert _waiting(shop, after=["gold_sales"]) == []


def test_a_window_that_published_once_counts_whatever_run_came_after_it(shop):
    _record(shop, 1, 2, "published")
    _record(shop, 2, 2, "failed")  # a later run of the same window, such as a replace that broke

    assert _waiting(shop, after=["gold_sales"]) == []


def test_a_run_that_has_no_window_does_not_count(shop):
    _record(shop, 1, None, "published")

    assert [item.reason for item in _waiting(shop, after=["gold_sales"])] == ["it has not run for the window"]


def test_the_window_is_the_instant_and_not_the_way_it_is_written(shop):
    _record(shop, 1, None, "published", span=("2026-08-02T05:30:00+05:30", "2026-08-03T05:30:00+05:30"))

    assert _waiting(shop, after=["gold_sales"]) == []


def test_a_window_that_is_not_the_window_but_overlaps_it_does_not_count(shop):
    _record(shop, 1, None, "published", span=("2026-08-02T00:00:00+00:00", "2026-08-02T12:00:00+00:00"))

    assert len(_waiting(shop, after=["gold_sales"])) == 1


@pytest.mark.parametrize("span", [
    None, "2026-08-02T00:00:00Z/2026-08-03T00:00:00Z", [], ["2026-08-02T00:00:00Z"],
    ["2026-08-02T00:00:00Z", "2026-08-03T00:00:00Z", "2026-08-04T00:00:00Z"], ["soon", "later"], [None, None],
    {"start": "2026-08-02T00:00:00Z", "end": "2026-08-03T00:00:00Z"}])
def test_a_window_that_is_not_two_instants_is_no_window(span):
    from local_data_platform.report.after import window_of

    assert window_of({"logical_window": span}) is None


@pytest.mark.parametrize("kind", [list, tuple])
@pytest.mark.parametrize("shown", [("2026-08-02T00:00:00Z", "2026-08-03T00:00:00Z"),
                                   ("2026-08-02T00:00:00+00:00", "2026-08-03T00:00:00+00:00"),
                                   ("2026-08-02T05:30:00+05:30", "2026-08-03T05:30:00+05:30")])
def test_a_window_of_two_instants_is_read_from_a_list_or_a_tuple_whatever_the_offset(kind, shown):
    from local_data_platform.report.after import window_of

    assert window_of({"logical_window": kind(shown)}) == _bounds(2)


def test_the_events_of_a_workflow_in_the_same_file_are_not_runs_of_a_pipeline(shop):
    from local_data_platform.events import RunEvent, read_jsonl
    from local_data_platform.report.after import runs_for_window

    _record(shop, 1, 2, "published")
    _record(shop, 2, 3, "published")  # another day
    moment = dt.datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    span = [bound.isoformat() for bound in _bounds(2)]
    started = [("workflow.started", {"window": span}), ("workflow.finished", {"status": "succeeded"})]
    workflow = [RunEvent(event_id=f"workflow-{seq}", type=kind, schema_version=1, ts=moment + dt.timedelta(seconds=seq),
                         run_id="run-workflow", attempt=1, seq=seq, payload={**payload, "pipeline": "ingest"})
                for seq, (kind, payload) in enumerate(started)]

    runs = runs_for_window(read_jsonl(shop / "config" / ".ldp" / "events.jsonl") + workflow, _bounds(2))

    assert [(run["run_id"], run["status"]) for run in runs] == [("run-gold_sales-1", "published")]


NO_SINK = "no run of it is recorded, so its config needs an iceberg or a jsonl sink in metadata.observability"


@pytest.mark.parametrize("sinks", [False, ["null"]])
def test_a_pipeline_whose_config_has_no_sink_that_keeps_runs_records_none_and_the_reason_says_so(shop, sinks):
    _pipeline(shop, observability=sinks)

    waiting = _waiting(shop, after=["gold_sales"])

    assert [item.reason for item in waiting] == [NO_SINK]


def test_runs_that_an_earlier_config_left_behind_do_not_make_a_pipeline_with_no_sink_one_that_has_not_run(shop):
    _record(shop, 1, 3, "published")  # in the events file that the default of a jsonl sink names
    _pipeline(shop, observability=False)

    assert [item.reason for item in _waiting(shop, after=["gold_sales"])] == [NO_SINK]


@pytest.mark.parametrize("sinks", [["jsonl"], ["iceberg"], ["null", "jsonl"]])
def test_a_pipeline_with_a_sink_that_keeps_runs_has_not_run_when_the_sink_has_none_of_the_window(shop, sinks):
    _pipeline(shop, observability=sinks)

    assert [item.reason for item in _waiting(shop, after=["gold_sales"])] == ["it has not run for the window"]


def test_only_the_pipelines_that_have_not_published_are_listed_in_the_order_of_the_report(shop):
    from local_data_platform.report.after import waiting_for
    from local_data_platform.report.spec import load_report

    other = json.loads((shop / "config" / "gold_sales.json").read_text())
    other["identifier"] = "gold_refunds"
    other["metadata"]["target"]["name"] = "gold_refunds"
    _write(shop / "config" / "gold_refunds.json", other)
    _record(shop, 1, 2, "published", name="gold_sales")
    path = _report(shop, after=["gold_refunds", "gold_sales"])

    waiting = waiting_for(load_report(path), _bounds(2))

    assert [item.pipeline for item in waiting] == ["gold_refunds"]


def test_a_pipeline_that_no_config_of_the_report_has_is_a_mistake_and_not_a_hold(shop):
    from local_data_platform.exceptions import ConfigError

    with pytest.raises(ConfigError) as caught:
        _waiting(shop, after=["gold_sales", "gold_typo"])

    assert str(caught.value) == ("metadata.after of the report daily names gold_typo, which no config in "
                                 "metadata.tables.config has")


@pytest.mark.parametrize("error", [OSError("cannot open the file"), RuntimeError("the catalog is down"),
                                   ValueError("a row that is not a run")])
def test_runs_that_cannot_be_read_hold_the_report_and_say_why_whatever_the_error_is(shop, monkeypatch, error):
    from local_data_platform.monitoring import ledger

    def broken(self, config):
        raise error

    monkeypatch.setattr(ledger.Ledger, "run_events", broken)

    waiting = _waiting(shop, after=["gold_sales"])

    assert [item.reason for item in waiting] == [f"its runs could not be read ({type(error).__name__}: {error})"]


def test_the_reason_that_runs_could_not_be_read_masks_what_looks_like_a_secret(shop, monkeypatch):
    from local_data_platform.monitoring import ledger

    def broken(self, config):
        raise OSError("cannot reach https://reader:hunter2@catalog.example.org/v1?token=abc123XYZ")

    monkeypatch.setattr(ledger.Ledger, "run_events", broken)

    (item,) = _waiting(shop, after=["gold_sales"])

    assert item.reason.startswith("its runs could not be read (OSError: cannot reach https://reader:")
    assert "hunter2" not in item.reason and "abc123XYZ" not in item.reason


def test_the_runs_table_is_read_once_for_all_the_pipelines_of_after(shop, monkeypatch):
    from local_data_platform import events

    _pipeline(shop, observability=["iceberg"])
    other = json.loads((shop / "config" / "gold_sales.json").read_text())
    other["identifier"] = "gold_refunds"
    other["metadata"]["target"]["name"] = "gold_refunds"
    _write(shop / "config" / "gold_refunds.json", other)
    _table(shop)  # the catalog exists, so its runs table is asked
    reads = []
    original = events.read_iceberg_events

    def counted(catalog, **keywords):
        reads.append(keywords["pipelines"])
        return original(catalog, **keywords)

    monkeypatch.setattr(events, "read_iceberg_events", counted)

    waiting = _waiting(shop, after=["gold_sales", "gold_refunds"])

    assert [item.pipeline for item in waiting] == ["gold_sales", "gold_refunds"]
    assert reads == [["gold_refunds", "gold_sales"]], "one read of the table, for both pipelines"


def test_the_check_only_reads_and_creates_nothing(shop):
    _record(shop, 1, 2, "published")
    before = sorted(str(path.relative_to(shop)) for path in shop.rglob("*"))

    _waiting(shop, after=["gold_sales"])

    assert sorted(str(path.relative_to(shop)) for path in shop.rglob("*")) == before


# ====================================================================== a report that is held back


def test_a_report_held_for_pipelines_says_which_and_why_in_a_message_that_names_the_window():
    from local_data_platform.report.health import ReportHeld, Waiting

    one = ReportHeld("daily", waiting=[Waiting("gold_sales", "it has not run for the window")], window=_span(2))
    two = ReportHeld("daily", waiting=[Waiting("a", "its last run for the window ended failed"),
                                       Waiting("b", "it has not run for the window")])

    assert str(one) == ("the report daily was held, because 1 pipeline of metadata.after has no published run for the "
                        f"window {_span(2)}:\n  - gold_sales: it has not run for the window")
    assert str(two) == ("the report daily was held, because 2 pipelines of metadata.after have no published run for "
                        "the window:\n  - a: its last run for the window ended failed\n"
                        "  - b: it has not run for the window")
    assert one.summary() == "waiting for gold_sales" and two.summary() == "waiting for a, b"
    assert [item.pipeline for item in two.waiting] == ["a", "b"] and one.window == _span(2) and two.window is None


def test_a_report_held_for_pipelines_and_for_stale_tables_says_both_in_the_message_and_in_the_summary():
    from local_data_platform.report.health import ReportHeld, TableHealth, Waiting

    stale = [TableHealth(table="gold.a", stale=True, reasons=("its last run ended failed",))]
    held = ReportHeld("daily", stale, waiting=[Waiting("gold_sales", "it has not run for the window")],
                      window=_span(2))

    assert str(held) == (
        "the report daily was held, because 1 pipeline of metadata.after has no published run for the window "
        f"{_span(2)}:\n  - gold_sales: it has not run for the window\n"
        "the report daily was held, because 1 table is stale and metadata.freshness says block\n"
        "  - gold.a: its last run ended failed")
    assert held.summary() == "waiting for gold_sales and 1 table is stale, gold.a"


def test_a_report_held_for_stale_tables_is_as_it_was_and_has_nobody_to_wait_for():
    from local_data_platform.report.health import ReportHeld, TableHealth

    held = ReportHeld("daily", [TableHealth(table="gold.a", stale=True, reasons=("its last run ended failed",))])

    assert str(held) == ("the report daily was held, because 1 table is stale and metadata.freshness says block\n"
                         "  - gold.a: its last run ended failed")
    assert held.waiting == () and held.window is None and held.summary() == "1 table is stale, gold.a"


def test_a_held_report_is_a_data_gate_so_the_command_line_exits_with_3():
    from local_data_platform.exceptions import DataQualityError
    from local_data_platform.report.health import ReportHeld, Waiting

    assert issubclass(ReportHeld, DataQualityError)
    assert isinstance(ReportHeld("daily", waiting=[Waiting("a", "x")]), DataQualityError)


def test_the_payload_of_a_hold_for_pipelines_has_each_pipeline_and_no_table(shop):
    from local_data_platform.report.build import held_payload
    from local_data_platform.report.health import ReportHeld, Waiting
    from local_data_platform.report.spec import load_report

    held = ReportHeld("daily", waiting=[Waiting("gold_sales", "it has not run for the window")], window=_span(2))

    payload = held_payload(load_report(_report(shop, after=["gold_sales"])), held, fmt="send", output="page, team",
                           window=_span(2), release=None, live=False)

    assert payload["waiting"] == [{"pipeline": "gold_sales", "reason": "it has not run for the window"}]
    assert payload["tables"] == [] and payload["rule"] is None and payload["window"] == _span(2)


def _send(root: Path, *extra: str, window: str | None = None) -> int:
    return main(["report", "send", str(root / "reports" / "daily.json"), "--window", window or _span(2), *extra])


def _load_day(root: Path, n: int = 2) -> None:
    assert main(["workflow", "run", str(_ingest(root)), "--window", _span(n)]) == 0


def test_a_send_that_waits_for_a_pipeline_is_held_with_exit_3_and_sends_and_records_no_delivery(shop, hooks, capsys):
    _report(shop, after=["gold_sales"])

    assert _send(shop) == 3

    err = capsys.readouterr().err
    assert ("the report daily was held, because 1 pipeline of metadata.after has no published run for the window "
            f"{_span(2)}:") in err
    assert "gold_sales: it has not run for the window" in err
    assert _posts(hooks) == 0 and not (shop / "reports" / "out" / "daily.html").exists()
    assert _types(_report_events(shop)) == ["run.started", "report.held", "run.finished"]
    held = _report_events(shop)[1]["payload"]
    assert held["waiting"] == [{"pipeline": "gold_sales", "reason": "it has not run for the window"}]
    assert held["window"] == _span(2) and held["format"] == "send" and held["output"] == "page, team"
    assert _report_events(shop)[2]["payload"]["status"] == "blocked_quality"


def test_a_hold_asks_before_any_query_runs(shop, hooks, monkeypatch):
    from local_data_platform.report import deliver

    _report(shop, after=["gold_sales"])
    monkeypatch.setattr(deliver, "build_report", lambda *a, **k: pytest.fail("a held report ran its queries"))

    assert _send(shop) == 3


def test_the_send_goes_out_once_the_pipeline_has_published_the_window(shop, hooks, capsys):
    _report(shop, after=["gold_sales"])
    assert _send(shop) == 3
    _load_day(shop)

    assert _send(shop) == 0

    assert _posts(hooks) == 1
    assert _types(_report_events(shop)).count("report.delivered") == 2
    assert "daily %s: 2 delivered" % _span(2) in capsys.readouterr().out


def test_a_pipeline_that_published_another_day_does_not_let_the_window_go(shop, hooks):
    _report(shop, after=["gold_sales"])
    _load_day(shop, 3)

    assert _send(shop) == 3 and _posts(hooks) == 0
    assert _send(shop, window=_span(3)) == 0 and _posts(hooks) == 1


def test_a_report_that_was_delivered_is_not_asked_about_its_pipelines_again(shop, hooks, monkeypatch, capsys):
    from local_data_platform.report import deliver

    _report(shop, after=["gold_sales"])
    _load_day(shop)
    assert _send(shop) == 0
    capsys.readouterr()
    monkeypatch.setattr(deliver, "waiting_for", lambda *a, **k: pytest.fail("a delivered window was asked about"))

    assert _send(shop) == 0

    assert _posts(hooks) == 1 and capsys.readouterr().out.count("already delivered at") == 2


def test_a_dry_run_is_held_too_and_records_nothing(shop, hooks, capsys):
    _report(shop, after=["gold_sales"])

    assert _send(shop, "--dry-run") == 3

    assert "metadata.after has no published run for the window" in capsys.readouterr().err
    assert _report_events(shop) == [] and _posts(hooks) == 0


def test_force_does_not_send_past_a_pipeline_that_has_not_published(shop, hooks):
    _report(shop, after=["gold_sales"])

    assert _send(shop, "--force") == 3
    assert _posts(hooks) == 0


def test_a_pipeline_that_is_not_in_the_folders_of_the_report_is_an_error_with_exit_1_and_a_failed_run(
        shop, hooks, capsys):
    _report(shop, after=["gold_typo"])

    assert _send(shop) == 1

    assert "names gold_typo, which no config in metadata.tables.config has" in capsys.readouterr().err
    assert _types(_report_events(shop)) == ["run.started", "run.failed", "run.finished"]
    failed = _report_events(shop)[1]["payload"]
    assert failed["stage"] == "after" and failed["error_type"] == "ConfigError"


def test_ldp_report_build_does_not_wait_for_the_pipelines_of_after(shop, tmp_path):
    _report(shop, after=["gold_sales"])
    _load_day(shop)
    out = shop / "page.html"
    (shop / "config" / ".ldp" / "events.jsonl").unlink()  # no run of the window is recorded any more

    assert main(["report", "build", str(shop / "reports" / "daily.json"), "--window", _span(2), "--out", str(out)]) == 0
    assert out.is_file()


# ====================================================================== --window yesterday


@pytest.mark.parametrize("shortcut", ["yesterday", "last-week", "last-month"])
def test_a_send_takes_the_window_shortcuts_in_the_time_zone_of_the_report(shop, hooks, capsys, shortcut):
    from zoneinfo import ZoneInfo

    from local_data_platform.spec import format_instant, parse_window

    _report(shop, timezone="Asia/Kolkata")
    _load_day(shop)
    capsys.readouterr()
    zone = ZoneInfo("Asia/Kolkata")
    first = parse_window(shortcut, timezone=zone)

    assert _send(shop, "--dry-run", window=shortcut) == 0

    last = parse_window(shortcut, timezone=zone)  # a clock that passed midnight in between is one of the two
    shown = capsys.readouterr().out
    spans = {f"{format_instant(start)}/{format_instant(end)}" for start, end in (first, last)}
    assert any(f"daily {span}: dry run" in shown for span in spans), (shown, spans)
    assert all("T18:30:00Z" in span.split("/")[0] and "T18:30:00Z" in span.split("/")[1] for span in spans)


def test_yesterday_is_the_day_of_the_report_and_not_the_day_of_utc(shop, hooks):
    from zoneinfo import ZoneInfo

    from local_data_platform.spec import format_instant, parse_window

    _report(shop, timezone="Pacific/Auckland")
    _load_day(shop)
    zone = ZoneInfo("Pacific/Auckland")
    first = parse_window("yesterday", timezone=zone)

    assert _send(shop, window="yesterday") == 0

    last = parse_window("yesterday", timezone=zone)
    windows = {item["payload"]["window"] for item in _report_events(shop) if item["type"] == "report.delivered"}
    assert windows <= {f"{format_instant(a)}/{format_instant(b)}" for a, b in (first, last)} and len(windows) == 1
    assert not windows & {f"{(dt.datetime.now(UTC) - dt.timedelta(days=1)).date()}T00:00:00Z/"
                          f"{dt.datetime.now(UTC).date()}T00:00:00Z"}


def test_a_report_has_no_schedule_of_its_own_and_the_message_says_where_to_put_it(shop):
    from local_data_platform.exceptions import ConfigError
    from local_data_platform.report.spec import load_report

    path = _report(shop)
    spec = json.loads(path.read_text())
    spec["metadata"]["schedule"] = "@daily"
    _write(path, spec)

    with pytest.raises(ConfigError) as caught:
        load_report(path)

    assert str(caught.value) == (
        "daily.json: metadata.schedule: a report has no schedule of its own. A workflow with one report task and a "
        "metadata.schedule runs it on a schedule, as ldp schedule tick runs a workflow")


# ====================================================================== the report task


def _table(root: Path, days: tuple[int, ...] = (2,)) -> None:
    """The table ``gold.gold_sales`` with 2 orders of 5 for each day, written with no pipeline run."""
    import pyarrow as pa

    from local_data_platform.format.iceberg import Iceberg

    rows = [dt.datetime.combine(_day(n), dt.time(10 + i)) for n in days for i in range(2)]
    table = pa.table({"placed_at": rows, "city": ["Pune"] * len(rows), "orders": [5] * len(rows)})
    Iceberg("gold_sales", {"identifier": "gold", "warehouse_path": str(root / "warehouse")}).put(table)


def _run(root: Path, name: str = "both.json", n: int = 2, *extra: str) -> int:
    return main(["workflow", "run", str(root / name), "--window", _span(n), *extra])


def _both(root: Path, **keywords) -> Path:
    """A workflow that loads the day and then sends the report."""
    return _workflow(root, "both.json", "both", [LOAD, {**SEND, "depends_on": ["sales"]}], **keywords)


def test_a_report_task_runs_the_report_for_the_window_of_the_run_and_sends_it(shop, hooks, capsys):
    _both(shop)

    assert _run(shop) == 0

    out = capsys.readouterr().out
    assert "daily page (file): delivered" in out and "daily team (slack): delivered" in out
    assert "succeeded: 1 published, 1 succeeded" in out
    assert _posts(hooks) == 1
    assert {item["payload"]["window"] for item in _report_events(shop) if item["type"] == "report.delivered"} == {
        _span(2)}
    page = (shop / "reports" / "out" / "daily.html").read_text()
    assert "Daily orders" in page and ">10<" in page.replace(" ", ""), "the 2 orders of 5 of the window"


def test_the_task_says_what_it_did_in_the_result_and_in_the_ledger_and_holds_no_address(shop, hooks):
    from local_data_platform.orchestration.runner import run_workflow

    _both(shop)

    result = run_workflow(shop / "both.json", window=_span(2))

    task = result.tasks["daily"]
    assert (task.kind, task.status) == ("report", "succeeded")
    assert task.detail["report"] == "daily" and task.detail["send"] is True
    assert task.detail["window"] == _span(2).split("/")
    assert (task.detail["delivered"], task.detail["skipped"], task.detail["failed"]) == (2, 0, 0)
    assert task.detail["destinations"] == [
        {"destination": "page", "type": "file", "status": "delivered", "attempts": 1},
        {"destination": "team", "type": "slack", "status": "delivered", "attempts": 1}]
    finished = [item for item in _ledger(shop) if item["type"] == "run.finished"
                and item["payload"].get("task_id") == "daily"]
    assert [item["payload"]["status"] for item in finished] == ["succeeded"]
    assert finished[0]["payload"]["destinations"] == task.detail["destinations"]
    assert SECRET not in json.dumps(_ledger(shop)) and SECRET not in json.dumps(_report_events(shop))


def test_the_events_of_the_send_go_to_the_sinks_of_the_report_with_the_workflow_the_run_and_the_task(shop, hooks):
    from local_data_platform.orchestration.runner import run_workflow

    _both(shop)

    result = run_workflow(shop / "both.json", window=_span(2))

    events = _report_events(shop)
    assert _types(events) == ["run.started", "report.delivered", "report.delivered", "run.finished"]
    for item in events:
        assert item["payload"]["workflow"] == "both" and item["payload"]["task_id"] == "daily"
        assert item["payload"]["workflow_run_id"] == result.run_id
    assert not [item for item in _ledger(shop) if item["type"].startswith("report.")], "the ledger has the task only"


def test_the_same_window_again_sends_nothing_and_the_task_is_skipped_as_a_duplicate(shop, hooks, capsys):
    _both(shop)
    assert _run(shop) == 0
    capsys.readouterr()

    assert _run(shop) == 0

    out = capsys.readouterr().out
    assert "daily page (file): already delivered" in out and "daily team (slack): already delivered" in out
    assert "succeeded: 2 skipped_duplicate" in out
    assert _posts(hooks) == 1
    assert _types(_report_events(shop)).count("report.delivered") == 2


def test_a_task_that_does_not_say_send_builds_the_report_and_sends_nothing(shop, hooks, capsys):
    task = {"id": "daily", "depends_on": ["sales"], "report": {"spec": "reports/daily.json"}}
    _workflow(shop, "check.json", "check", [LOAD, task])

    assert _run(shop, "check.json") == 0

    assert _posts(hooks) == 0 and not (shop / "reports" / "out").exists()
    assert "report" in capsys.readouterr().out
    assert _types(_report_events(shop)) == ["run.started", "report.rendered", "run.finished"]
    assert not [item for item in _report_events(shop) if item["type"] == "report.delivered"]
    for item in _report_events(shop):
        assert item["payload"]["workflow"] == "check" and item["payload"]["task_id"] == "daily"


def test_a_task_with_send_false_is_the_same_as_one_with_no_send(shop, hooks):
    from local_data_platform.orchestration.runner import run_workflow

    task = {"id": "daily", "depends_on": ["sales"], "report": {"spec": "reports/daily.json", "send": False}}
    _workflow(shop, "check.json", "check", [LOAD, task])

    result = run_workflow(shop / "check.json", window=_span(2))

    task = result.tasks["daily"]
    assert task.status == "succeeded" and task.detail["send"] is False
    assert (task.detail["queries"], task.detail["blocks"], task.detail["stale"]) == (1, 1, [])
    assert _posts(hooks) == 0


def test_a_task_that_only_builds_lists_the_stale_tables_in_its_detail_when_the_rule_only_warns(shop, hooks):
    from local_data_platform.orchestration.runner import run_workflow

    _table(shop)
    _record(shop, 1, 2, "published")
    _record(shop, 2, 3, "failed")  # the last run of the pipeline broke
    _report(shop, freshness={"on_stale": "warn"})
    _workflow(shop, "check.json", "check", [{"id": "daily", "report": {"spec": "reports/daily.json"}}])

    result = run_workflow(shop / "check.json", window=_span(2))

    task = result.tasks["daily"]
    assert task.status == "succeeded" and task.detail["stale"] == ["gold.gold_sales"]


def test_a_destination_that_failed_is_counted_in_the_detail_of_the_task_and_named_in_its_error(shop, hooks):
    from local_data_platform.orchestration.runner import run_workflow

    _both(shop)
    hooks.status = 500

    result = run_workflow(shop / "both.json", window=_span(2))

    task = result.tasks["daily"]
    assert task.status == "failed" and result.exit_code == 1
    assert (task.detail["delivered"], task.detail["skipped"], task.detail["failed"]) == (1, 0, 1)
    assert [(item["destination"], item["status"]) for item in task.detail["destinations"]] == [
        ("page", "delivered"), ("team", "failed")]
    assert "did not reach 1 of 2 destinations, and the others got it:\n  - team (slack): " in task.error


def test_a_destination_that_does_not_answer_fails_the_task_and_the_others_still_got_the_report(shop, hooks, capsys):
    _both(shop)
    hooks.status = 500

    assert _run(shop) == 1

    captured = capsys.readouterr()
    assert "daily team (slack): not delivered" in captured.out and "daily page (file): delivered" in captured.out
    assert "TaskFailed: the report daily did not reach 1 of 2 destinations, and the others got it" in captured.err
    assert (shop / "reports" / "out" / "daily.html").is_file()
    assert _posts(hooks) == 4, "the first post and the 3 that the notifier sends again after a 5xx answer"


def test_the_next_run_of_the_window_sends_only_to_the_destination_that_failed(shop, hooks, capsys):
    _both(shop)
    hooks.status = 500
    assert _run(shop) == 1
    hooks.status = 200
    hooks.received.clear()
    capsys.readouterr()

    assert _run(shop) == 0

    out = capsys.readouterr().out
    assert "daily page (file): already delivered" in out and "daily team (slack): delivered" in out
    assert _posts(hooks) == 1


def test_a_report_held_by_its_freshness_gate_is_a_blocked_task_with_exit_3_and_nothing_is_sent(shop, hooks, capsys):
    _table(shop)
    _record(shop, 1, 2, "published")
    _record(shop, 2, 3, "failed")  # the last run of the pipeline broke
    _report(shop, freshness={"on_stale": "block"})
    _workflow(shop, "send_only.json", "send_only", [SEND])

    assert _run(shop, "send_only.json") == 3

    captured = capsys.readouterr()
    assert "TaskBlocked: the report daily was held, because 1 table is stale and metadata.freshness says block" in (
        captured.err)
    assert "blocked_quality" in captured.out
    assert _posts(hooks) == 0
    assert _types(_report_events(shop)) == ["run.started", "report.held", "run.finished"]


def test_a_report_held_for_a_pipeline_is_a_blocked_task_with_exit_3_and_the_waiting_pipeline_in_its_detail(
        shop, hooks, capsys):
    from local_data_platform.orchestration.runner import run_workflow

    _table(shop)
    _report(shop, after=["gold_sales"])
    _workflow(shop, "send_only.json", "send_only", [SEND])

    result = run_workflow(shop / "send_only.json", window=_span(2))

    task = result.tasks["daily"]
    assert (task.status, result.exit_code) == ("blocked_quality", 3)
    assert task.detail["held"] == "waiting for gold_sales" and task.detail["report"] == "daily"
    assert "metadata.after has no published run for the window" in task.error
    assert _posts(hooks) == 0


def test_a_task_whose_report_has_no_destination_is_not_valid_and_says_what_to_change(shop, hooks):
    from local_data_platform.exceptions import ConfigError
    from local_data_platform.orchestration.runner import run_workflow

    spec = json.loads(_report(shop).read_text())
    del spec["metadata"]["delivery"]
    _write(shop / "reports" / "daily.json", spec)
    _both(shop)

    with pytest.raises(ConfigError) as caught:
        run_workflow(shop / "both.json", window=_span(2))

    assert ("task 'daily': the report daily has no metadata.delivery, so there is nowhere to send it. Add a "
            "destination, or set send to false") in str(caught.value)


def test_a_query_that_does_not_run_fails_the_task_and_sends_nothing(shop, hooks):
    from local_data_platform.orchestration.runner import run_workflow

    _table(shop)
    spec = json.loads((shop / "reports" / "daily.json").read_text())
    spec["metadata"]["queries"]["totals"]["sql"] = "SELECT nothing FROM gold.gold_sales"
    _write(shop / "reports" / "daily.json", spec)
    _workflow(shop, "send_only.json", "send_only", [SEND])

    result = run_workflow(shop / "send_only.json", window=_span(2))

    task = result.tasks["daily"]
    assert task.status == "failed" and "the report daily has 1 problem and was not built" in task.error
    assert _posts(hooks) == 0 and result.exit_code == 1


def test_a_replace_does_not_send_the_report_again(shop, hooks):
    from local_data_platform.orchestration.runner import run_workflow

    _both(shop)
    run_workflow(shop / "both.json", window=_span(2))

    result = run_workflow(shop / "both.json", window=_span(2), replace=True)

    assert result.tasks["daily"].status == "skipped_duplicate" and _posts(hooks) == 1


# ====================================================================== the value of the task


@pytest.mark.parametrize("value", [
    {"spec": "reports/daily.json"},
    {"spec": "reports/daily.json", "send": True},
    {"spec": "reports/daily.json", "send": False},
    {"spec": "r.json"},
])
def test_a_report_task_needs_a_spec_and_may_say_send(value):
    from local_data_platform.orchestration.report_task import validate

    assert validate(value) == []


@pytest.mark.parametrize("value, problems", [
    ("reports/daily.json", [("", "must be an object with a 'spec', the path of a report file")]),
    ([], [("", "must be an object with a 'spec', the path of a report file")]),
    (None, [("", "must be an object with a 'spec', the path of a report file")]),
    ({}, [(".spec", "is required: the path of a report file")]),
    ({"spec": ""}, [(".spec", "is required: the path of a report file")]),
    ({"spec": "  "}, [(".spec", "is required: the path of a report file")]),
    ({"spec": 5}, [(".spec", "is required: the path of a report file")]),
    ({"spec": "a.json", "send": "yes"}, [(".send", "must be true or false, got 'yes'")]),
    ({"spec": "a.json", "send": 1}, [(".send", "must be true or false, got 1")]),
    ({"spec": "a.json", "sned": True}, [("", "has unknown keys ['sned']; allowed: ['spec', 'send']")]),
    ({"spec": 5, "send": 2, "x": 1}, [("", "has unknown keys ['x']; allowed: ['spec', 'send']"),
                                      (".spec", "is required: the path of a report file"),
                                      (".send", "must be true or false, got 2")]),
])
def test_a_report_task_with_a_value_that_is_not_one_says_what_is_wrong(value, problems):
    from local_data_platform.orchestration.report_task import validate

    assert validate(value) == problems


def test_a_workflow_file_with_a_bad_report_task_is_checked_without_reading_any_file(tmp_path):
    from local_data_platform.spec import validate_spec

    spec = {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "w", "metadata": {"window": {"size": "day"}},
            "tasks": [{"id": "daily", "report": {"send": "yes"}}]}

    problems = [str(error) for error in validate_spec(spec)]

    assert "tasks[0].report.spec: is required: the path of a report file" in problems
    assert "tasks[0].report.send: must be true or false, got 'yes'" in problems


def test_the_report_kind_is_a_built_in_kind_that_is_listed_in_its_fixed_place():
    from local_data_platform.orchestration import tasks

    assert tasks.BUILTIN_KINDS == ("pipeline", "maintain", "dbt", "gold", "report")
    assert tasks.task_kinds()[:5] == ["pipeline", "maintain", "dbt", "gold", "report"]
    kind = tasks.get_task_kind("report")
    assert kind.emits_events is False and kind.changes_data is True
    assert kind.description.startswith("Build a report for the window of the workflow run")


def test_the_schema_of_the_task_has_the_spec_and_send_and_nothing_else():
    from local_data_platform.spec import kind_schema

    schema = kind_schema("Workflow")["$defs"]["task"]["properties"]["report"]

    assert schema["required"] == ["spec"] and schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"spec", "send"}
    assert schema["properties"]["send"] == {
        "type": "boolean", "default": False,
        "description": "Send the report. Without it the task builds the report, which runs its queries and applies its "
                       "freshness gate, and sends nothing."}
    assert schema["properties"]["spec"]["minLength"] == 1


def test_a_workflow_with_a_report_task_is_valid_for_the_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    from local_data_platform.spec import kind_schema

    validator = jsonschema.Draft202012Validator(kind_schema("Workflow"))
    good = {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "w", "metadata": {"window": {"size": "day"}},
            "tasks": [{"id": "daily", "report": {"spec": "reports/daily.json", "send": True}}]}
    bad = json.loads(json.dumps(good))
    bad["tasks"][0]["report"] = {"send": True, "spec": 5, "extra": 1}

    assert list(validator.iter_errors(good)) == []
    assert len(list(validator.iter_errors(bad))) >= 2


# ====================================================================== what ldp workflow plan says


def _plan(root: Path, name: str = "both.json", **keywords) -> dict:
    from local_data_platform.orchestration.workflow import plan_workflow

    return plan_workflow(root / name, window=_span(2), **keywords)


def _daily(plan: dict) -> dict:
    return next(item for item in plan["tasks"] if item["id"] == "daily")


def test_the_plan_of_a_report_task_says_the_report_the_destinations_and_the_zone(shop):
    _both(shop)

    plan = _plan(shop)

    assert plan["valid"] is True and plan["errors"] == []
    entry = _daily(plan)
    assert (entry["spec"], entry["send"], entry["report"], entry["timezone"]) == (
        "reports/daily.json", True, "daily", "UTC")
    assert entry["target"] == "page (file), team (slack)"
    assert entry["destinations"] == [{"destination": "page", "type": "file"}, {"destination": "team", "type": "slack"}]
    assert "after" not in entry


def test_the_plan_names_the_pipelines_that_the_report_waits_for(shop):
    _report(shop, after=["gold_sales"])
    _both(shop)

    assert _daily(_plan(shop))["after"] == ["gold_sales"]


def test_the_plan_of_a_task_that_only_builds_says_so_and_has_no_destination(shop):
    task = {"id": "daily", "depends_on": ["sales"], "report": {"spec": "reports/daily.json"}}
    _workflow(shop, "check.json", "check", [LOAD, task])

    plan = _plan(shop, "check.json")

    assert plan["valid"] is True
    entry = _daily(plan)
    assert entry["send"] is False and entry["target"] == "a build, nothing is sent" and "destinations" not in entry
    assert "task 'daily': send is not true, so the task builds the report for the window and sends nothing" in (
        plan["warnings"])


def test_the_plan_of_a_report_file_that_is_not_there_is_an_error(shop):
    _workflow(shop, "missing.json", "missing", [{"id": "daily", "report": {"spec": "reports/nothing.json"}}])

    plan = _plan(shop, "missing.json")

    assert plan["valid"] is False and len(plan["errors"]) == 1
    assert plan["errors"][0].startswith("task 'daily': ") and "nothing.json" in plan["errors"][0]


def test_the_plan_of_a_report_that_is_not_valid_lists_its_problems(shop):
    spec = json.loads((shop / "reports" / "daily.json").read_text())
    spec["metadata"]["blocks"] = [{"type": "kpi", "query": "nothing", "value": "n"}]
    _write(shop / "reports" / "daily.json", spec)
    _both(shop)

    plan = _plan(shop)

    assert plan["valid"] is False
    assert any("daily.json" in message and "nothing" in message for message in plan["errors"])


def test_the_plan_of_a_send_with_nowhere_to_send_it_is_an_error_and_a_build_is_not(shop):
    spec = json.loads((shop / "reports" / "daily.json").read_text())
    del spec["metadata"]["delivery"]
    _write(shop / "reports" / "daily.json", spec)
    _both(shop)
    task = {"id": "daily", "depends_on": ["sales"], "report": {"spec": "reports/daily.json", "send": False}}
    _workflow(shop, "check.json", "check", [LOAD, task])

    sends = _plan(shop)
    builds = _plan(shop, "check.json")

    assert sends["errors"] == ["task 'daily': the report daily has no metadata.delivery, so there is nowhere to send "
                               "it. Add a destination, or set send to false"]
    assert builds["valid"] is True


def test_the_plan_of_a_report_that_waits_for_a_pipeline_that_it_does_not_have_is_an_error(shop):
    _report(shop, after=["gold_sales", "gold_typo"])
    _both(shop)

    plan = _plan(shop)

    assert plan["errors"] == ["task 'daily': metadata.after of the report daily names gold_typo, which no config in "
                              "metadata.tables.config has"]


def test_the_plan_notes_a_report_that_cannot_remember_a_delivery(shop):
    _report(shop, observability=False)
    _both(shop)

    plan = _plan(shop)

    assert plan["valid"] is True
    assert ("task 'daily': the report daily has no sink in metadata.observability, so it cannot remember a delivery, "
            "and a run of the same window sends it again") in plan["warnings"]


def test_the_plan_notes_that_the_report_and_the_workflow_count_their_days_in_different_zones(shop):
    _report(shop, timezone="Asia/Kolkata")
    _both(shop)
    _both(shop, timezone="Asia/Kolkata")

    same = _plan(shop)
    _both(shop)
    apart = _plan(shop)

    assert not [message for message in same["warnings"] if "counts its days in" in message]
    assert ("task 'daily': the report counts its days in Asia/Kolkata and the windows of this workflow are counted in "
            "UTC, so its queries see the window as the wall-clock times of Asia/Kolkata. Set metadata.window.timezone "
            "of the workflow to Asia/Kolkata for a window that is a day of the report") in apart["warnings"]


def test_the_plan_of_a_replace_says_that_the_report_is_not_sent_again(shop):
    _both(shop)

    plan = _plan(shop, replace=True)

    assert ("task 'daily': a replace does not send the report again, because a delivery is made once for each "
            "window. ldp report send --force sends it again") in plan["warnings"]
    assert not [message for message in _plan(shop)["warnings"] if "a replace" in message]


def test_the_plan_warns_when_the_report_reads_a_table_that_a_task_writes_and_nothing_orders_them(shop):
    _workflow(shop, "loose.json", "loose", [LOAD, SEND])

    loose = _plan(shop, "loose.json")
    _both(shop)
    ordered = _plan(shop)

    warning = ("task 'daily' reads the table gold.gold_sales, which task 'sales' writes, and no depends_on connects "
               "them, so they may run in either order")
    assert warning in loose["warnings"] and warning not in ordered["warnings"]


def test_the_plan_does_not_warn_about_a_table_that_the_allow_list_of_the_report_leaves_out(shop):
    spec = json.loads((shop / "reports" / "daily.json").read_text())
    spec["metadata"]["tables"]["allow"] = ["other.*"]
    _write(shop / "reports" / "daily.json", spec)
    _workflow(shop, "loose.json", "loose", [LOAD, SEND])

    assert not [message for message in _plan(shop, "loose.json")["warnings"] if "reads the table" in message]


def test_ldp_workflow_plan_prints_the_row_of_the_report_task(shop, capsys):
    _both(shop)

    assert main(["workflow", "plan", str(shop / "both.json"), "--window", _span(2)]) == 0

    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if " report " in line)
    assert "daily" in row and "sales" in row and "reports/daily.json" in row and "page (file), team (slack)" in row


# ====================================================================== ldp schedule tick, with a fixed clock


def _held_project(root: Path) -> None:
    """A report that waits for the pipeline, whose workflow runs before the one that loads the day."""
    _report(root, after=["gold_sales"])
    _own(root, name="a_send.json")
    _ingest(root)


def _tick_cli(root: Path, n: int, clock: str = "02:05") -> int:
    return main(["schedule", "tick", str(root), "--now", _at(n, clock)])


def test_a_held_report_is_run_again_by_the_next_tick_and_goes_out_once_the_pipeline_has_published(shop, hooks, capsys):
    _held_project(shop)

    assert _tick_cli(shop, 3, "02:05") == 3

    first = capsys.readouterr()
    assert f"daily_report  {_span(2)}  blocked" in first.out and f"ingest  {_span(2)}  published" in first.out
    assert "1 published, 0 skipped, 1 blocked, 0 failed" in first.out
    assert "daily: TaskBlocked: the report daily was held, because 1 pipeline of metadata.after has no published run" \
        in first.err
    assert "The next tick runs the windows that failed or were blocked again" in first.err
    assert _posts(hooks) == 0 and _types(_report_events(shop)) == ["run.started", "report.held", "run.finished"]

    assert _tick_cli(shop, 3, "02:10") == 0

    second = capsys.readouterr()
    assert f"daily_report  {_span(2)}  published  (attempt 2)" in second.out
    assert _posts(hooks) == 1 and _types(_report_events(shop)).count("report.delivered") == 2


def test_the_window_that_was_held_is_the_same_run_and_its_next_attempt_in_the_ledger(shop, hooks):
    _held_project(shop)

    _tick(shop, 3, "02:05")
    _tick(shop, 3, "02:10")

    started = [item for item in _ledger(shop) if item["type"] == "workflow.started"
               and item["payload"]["workflow"] == "daily_report"]
    assert [item["attempt"] for item in started] == [1, 2] and started[0]["run_id"] == started[1]["run_id"]
    finished = [item["payload"]["status"] for item in _ledger(shop) if item["type"] == "workflow.finished"
                and item["payload"]["workflow"] == "daily_report"]
    assert finished == ["blocked_quality", "succeeded"]


def test_a_held_window_has_no_delivery_so_it_is_not_done_and_each_tick_asks_again(shop, hooks):
    _report(shop, after=["gold_sales"])
    _own(shop, name="a_send.json")  # the pipeline never runs

    for minute in ("02:05", "02:10", "02:15"):
        assert _tick(shop, 3, minute).exit_code == 3

    assert _posts(hooks) == 0
    assert _types(_report_events(shop)).count("report.held") == 3
    assert not [item for item in _report_events(shop) if item["type"] == "report.delivered"]


def test_a_tick_after_the_delivery_sends_nothing_and_nothing_is_held(shop, hooks):
    _held_project(shop)
    _tick(shop, 3, "02:05")
    _tick(shop, 3, "02:10")

    third = _tick(shop, 3, "02:15")

    assert third.exit_code == 0 and third.counts() == {"published": 0, "skipped": 2, "blocked": 0, "failed": 0}
    assert _posts(hooks) == 1 and _types(_report_events(shop)).count("report.held") == 1


def test_a_destination_that_failed_is_sent_to_again_by_the_next_tick_and_the_others_are_not(shop, hooks):
    _own(shop)
    _ingest(shop)
    hooks.status = 500

    first = _tick(shop, 3, "02:05")

    assert first.exit_code == 1 and first.counts()["failed"] == 1
    assert (shop / "reports" / "out" / "daily.html").is_file()
    delivered = [item["payload"]["destination"] for item in _report_events(shop) if item["type"] == "report.delivered"]
    assert delivered == ["page"]
    hooks.status = 200
    hooks.received.clear()

    second = _tick(shop, 3, "02:10")

    assert second.exit_code == 0 and _posts(hooks) == 1
    delivered = [item["payload"]["destination"] for item in _report_events(shop) if item["type"] == "report.delivered"]
    assert delivered == ["page", "team"]


def test_a_report_after_the_pipelines_in_one_workflow_goes_out_in_the_same_tick(shop, hooks):
    _both(shop)

    result = _tick(shop, 3)

    assert result.exit_code == 0 and result.counts()["published"] == 1
    assert _posts(hooks) == 1
    tasks = [item["payload"] for item in _ledger(shop) if item["type"] == "workflow.finished"]
    assert tasks[0]["tasks"] == {"sales": "published", "daily": "succeeded"}


def test_a_report_in_the_workflow_of_the_pipelines_is_held_for_the_pipelines_of_after_that_it_does_not_run(shop, hooks):
    _report(shop, after=["gold_sales"])
    _workflow(shop, "alone.json", "alone", [SEND])  # nothing loads gold_sales

    assert _tick(shop, 3).exit_code == 3
    assert _posts(hooks) == 0


def test_the_days_of_the_workflow_are_the_days_of_the_report_when_both_count_them_in_the_same_zone(shop, hooks):
    _table(shop, (1, 2))
    _report(shop, timezone="Asia/Kolkata")
    _own(shop, timezone="Asia/Kolkata")

    result = _tick(shop, 3, "02:05")  # 07:35 in Kolkata on day 3: the fire of that midnight owns day 2 there

    assert result.exit_code == 0
    windows = {item["payload"]["window"] for item in _report_events(shop) if item["type"] == "report.delivered"}
    assert windows == {"2026-08-01T18:30:00Z/2026-08-02T18:30:00Z"}
    assert all(item["payload"]["timezone"] == "Asia/Kolkata" for item in _report_events(shop)
               if item["type"] == "report.delivered")


def test_a_missed_day_is_caught_up_by_sending_the_window_of_that_day_and_a_repeat_sends_nothing(shop, hooks, capsys):
    _own(shop)
    _ingest(shop)
    _tick(shop, 3)  # day 2 went out, and the tick of day 4 never ran, so day 3 was missed
    assert _posts(hooks) == 1
    _load_day(shop, 3)
    capsys.readouterr()

    assert _send(shop, window=_span(3)) == 0
    assert _posts(hooks) == 2
    capsys.readouterr()
    assert _send(shop, window=_span(3)) == 0

    assert _posts(hooks) == 2
    assert capsys.readouterr().out.count("already delivered at") == 2


def test_ldp_schedule_list_shows_the_workflow_of_the_report_with_its_schedule(shop, hooks, capsys):
    _own(shop)

    assert main(["schedule", "list", str(shop), "--now", _at(3)]) == 0

    out = capsys.readouterr().out
    assert "daily_report  Workflow" in out and "schedule     @daily, in UTC, windows of one day" in out


def test_a_tick_that_finds_a_report_that_is_not_valid_says_so_and_runs_the_others(shop, hooks):
    _own(shop)
    _ingest(shop)
    spec = json.loads((shop / "reports" / "daily.json").read_text())
    spec["metadata"]["blocks"] = []
    _write(shop / "reports" / "daily.json", spec)

    result = _tick(shop, 3)

    assert result.exit_code == 1 and result.counts()["published"] == 1
    broken = next(item for item in result.workflows if item.workflow == "daily_report")
    assert broken.error is not None and "is not valid, so no window ran" in broken.error
    assert _posts(hooks) == 0


# ====================================================================== what the README shows


def _readme_blocks(language: str, needle: str) -> list[str]:
    from tests.docs_runner import parse_blocks

    return [block.code for block in parse_blocks(README) if block.language == language and needle in block.code]


def test_the_crontab_line_of_the_readme_is_a_line_that_ldp_runs(shop, hooks, capsys):
    from local_data_platform.orchestration import windows

    (block,) = _readme_blocks("bash", "ldp report send /path/to/reports/daily_sales.json")
    fields = block.strip().split(None, 5)
    windows.parse_schedule(" ".join(fields[:5]))  # it is a cron expression that the scheduler reads too
    argv = shlex.split(fields[5])
    assert fields[:5] == ["0", "8", "*", "*", "*"]
    assert argv[:3] == ["/path/to/.venv/bin/ldp", "report", "send"] and argv[-2:] == ["--window", "yesterday"]
    _load_day(shop)
    capsys.readouterr()
    argv[3] = str(shop / "reports" / "daily.json")

    assert main([*argv[1:], "--dry-run"]) == 0

    assert "dry run, 2 would be sent" in capsys.readouterr().out


def test_the_workflow_of_the_readme_is_a_valid_report_on_its_own_schedule():
    from local_data_platform.spec import validate_spec

    (block,) = _readme_blocks("json", '"identifier": "daily_sales_report"')
    workflow = json.loads(block)

    assert [str(error) for error in validate_spec(workflow)] == []
    assert workflow["metadata"]["schedule"] == "0 8 * * *" and workflow["tasks"][0]["report"]["send"] is True
    assert workflow["metadata"]["window"] == {"size": "day", "timezone": "Asia/Kolkata"}


def test_the_task_of_the_readme_that_follows_the_gold_task_is_a_valid_task():
    from local_data_platform.orchestration.report_task import validate

    (block,) = _readme_blocks("json", '"depends_on": ["gold"], "report"')
    task = json.loads(block)

    assert validate(task["report"]) == [] and task["depends_on"] == ["gold"]


def test_the_runs_of_a_pipeline_that_logs_to_the_runs_table_of_iceberg_are_found_too(shop, hooks):
    _pipeline(shop, observability=["iceberg"])
    _report(shop, after=["gold_sales"])
    _own(shop, name="a_send.json")
    _ingest(shop)

    first = _tick(shop, 3, "02:05")

    assert first.exit_code == 3 and _posts(hooks) == 0
    held = [item["payload"] for item in _report_events(shop) if item["type"] == "report.held"]
    assert held[0]["waiting"] == [{"pipeline": "gold_sales", "reason": "it has not run for the window"}]

    second = _tick(shop, 3, "02:10")

    assert second.exit_code == 0 and _posts(hooks) == 1


@pytest.mark.parametrize("first", [
    "local_data_platform.orchestration.report_task", "local_data_platform.orchestration.tasks",
    "local_data_platform.orchestration.runner", "local_data_platform.orchestration",
    "local_data_platform.report.after", "local_data_platform.report.deliver", "local_data_platform.spec",
])
def test_the_report_kind_is_registered_whichever_module_is_imported_first(first):
    """The report kind imports from the registry and the registry imports it, so the order must not matter."""
    probe = (f"import {first}\nfrom local_data_platform.orchestration import tasks\n"
             "import local_data_platform.orchestration.report_task\nprint(tasks.task_kinds())\n")

    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=False)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "['pipeline', 'maintain', 'dbt', 'gold', 'report']"


def test_importing_the_report_task_needs_only_the_standard_library_and_not_duckdb_or_requests():
    probe = ("import sys\nimport local_data_platform.orchestration.report_task\n"
             "bad = [name for name in ('duckdb', 'requests', 'pyiceberg') if name in sys.modules]\nprint(bad)\n")

    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120, check=False)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


def test_a_backfill_sends_the_report_of_each_window_it_runs_and_a_second_backfill_sends_nothing(shop, hooks):
    _both(shop)
    command = ["backfill", str(shop / "both.json"), "--from", "2026-08-02", "--to", "2026-08-04"]

    assert main(command) == 0

    assert _posts(hooks) == 2
    sent = [item["payload"]["window"] for item in _report_events(shop) if item["type"] == "report.delivered"
            and item["payload"]["destination"] == "team"]
    assert sent == [_span(2), _span(3)]
    assert main(command) == 0 and _posts(hooks) == 2


def test_a_workflow_that_catches_up_sends_the_report_of_each_window_that_it_missed(shop, hooks):
    _both(shop, extra={"catchup": True, "start": _day(1).isoformat()})

    result = _tick(shop, 4, "02:05")

    assert result.exit_code == 0 and _posts(hooks) == 3
    assert _tick(shop, 4, "02:10").exit_code == 0 and _posts(hooks) == 3
