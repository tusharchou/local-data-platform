"""Run events: ids, the envelope, redaction, and every sink (null, memory, JSONL, OpenLineage, Iceberg, multi)."""

import argparse
import dataclasses
import datetime as dt
import http.server
import json
import logging
import threading
import uuid
from pathlib import Path

import pytest

from local_data_platform import events
from local_data_platform.events import (
    EventSink,
    IcebergSink,
    JsonlSink,
    MemorySink,
    MultiSink,
    NullSink,
    OpenLineageSink,
    RunEmitter,
    RunEvent,
    dataset_ref,
    load_run_events,
    quality_payload,
    read_iceberg_events,
    read_jsonl,
    read_openlineage,
    redact_text,
    schema_payload,
    sink_from_config,
    sink_specs,
    summarize_runs,
    uuid7,
)
from local_data_platform.exceptions import ConfigError, LDPError
from local_data_platform.quality import AcceptedValues, NotNull, Range, Unique, run_checks

T0 = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc)


def _event(type_="run.started", seq=0, run_id="r1", attempt=1, payload=None, ts=T0) -> RunEvent:
    return RunEvent(event_id=uuid7(), type=type_, schema_version=1, ts=ts, run_id=run_id, attempt=attempt, seq=seq,
                    table_uuid=None, payload=payload or {})


def _run_events(run_id=None, *, status="published", window=None, rows=6) -> list[RunEvent]:
    """A complete, realistic event sequence for one run, as Pipeline.run emits it."""
    run_id = run_id or uuid7()
    context = {"pipeline": "rides", "table": "ns.rides"}
    started = {"mode": "append", "publish": "direct", "idempotency_key": None, "spec_hash": "abc",
               "logical_window": window, "ldp_version": "0.1.1",
               "source": {"namespace": "file", "name": "/data/rides.csv", "format": "CSV"},
               "target": {"namespace": "file:///wh", "name": "ns.rides", "format": "ICEBERG",
                          "table_identifier": "ns.rides"}}
    schema = [{"name": "ride_id", "type": "int64"}, {"name": "fare", "type": "double"}]
    quality = {"rows": rows, "schema": schema, "null_counts": {"ride_id": 0, "fare": 1}, "on_failure": "fail",
               "passed": status != "blocked_quality", "checks_run": 1,
               "checks_failed": 1 if status == "blocked_quality" else 0,
               "results": [{"name": "not_null(ride_id)", "column": "ride_id", "passed": status != "blocked_quality",
                            "failing_rows": 0, "details": "ok", "metrics": {"null_counts": {"ride_id": 0}}}]}
    sequence = [("run.started", started), ("run.extracted", {"rows_read": rows, "schema": schema}),
                ("quality.evaluated", quality)]
    if status == "published":
        sequence.append(("run.published", {"rows_written": rows, "snapshot_id": 42,
                                           "snapshot_summary": {"added-files-size": "1234", "added-data-files": "2"}}))
    elif status == "blocked_quality":
        sequence.append(("run.blocked_quality", {"checks_failed": 1, "failures": ["not_null(ride_id)"]}))
    elif status == "failed":
        sequence.append(("run.failed", {"stage": "write", "error_type": "ValueError", "message": "boom"}))
    sequence.append(("run.finished", {"status": status, "duration_s": 0.5, "rows_read": rows,
                                      "rows_written": rows if status == "published" else 0,
                                      "published_snapshot_id": 42 if status == "published" else None}))
    return [_event(kind, seq, run_id, payload={**context, **payload}, ts=T0 + dt.timedelta(seconds=seq))
            for seq, (kind, payload) in enumerate(sequence)]


# ---------------------------------------------------------------------- uuid7


def test_uuid7_is_a_version_7_rfc_uuid_in_creation_order():
    ids = [uuid7() for _ in range(2000)]
    parsed = [uuid.UUID(value) for value in ids]
    assert all(item.version == 7 and item.variant == uuid.RFC_4122 for item in parsed)
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert all(len(value) == 36 for value in ids)


def test_the_fallback_uuid7_encodes_the_time_and_stays_monotonic(monkeypatch):
    monkeypatch.setattr(events, "_uuid7_last_ms", 0)
    monkeypatch.setattr(events, "_uuid7_counter", 0)
    now_ms = 1_790_000_000_000
    first = events._uuid7_fallback(now_ms)
    assert first.version == 7 and first.variant == uuid.RFC_4122
    assert first.int >> 80 == now_ms
    same_ms = [events._uuid7_fallback(now_ms) for _ in range(500)]
    assert [first, *same_ms] == sorted([first, *same_ms]), "ids within one millisecond must still sort"
    earlier = events._uuid7_fallback(now_ms - 5_000)  # the clock went backwards
    assert earlier > same_ms[-1]


def test_the_fallback_uuid7_moves_to_the_next_millisecond_when_the_counter_overflows(monkeypatch):
    monkeypatch.setattr(events, "_uuid7_last_ms", 1_000)
    monkeypatch.setattr(events, "_uuid7_counter", events._COUNTER_MAX)
    value = events._uuid7_fallback(1_000)
    assert value.int >> 80 == 1_001


def test_uuid7_uses_the_fallback_when_python_has_no_uuid7(monkeypatch):
    monkeypatch.delattr(uuid, "uuid7", raising=False)
    value = uuid.UUID(uuid7())
    assert value.version == 7


# ---------------------------------------------------------------------- envelope


def test_a_run_event_round_trips_through_json_and_ignores_unknown_fields():
    event = _event("run.published", 3, payload={"rows_written": 6, "when": dt.date(2026, 9, 30)})
    data = json.loads(event.to_json())
    assert data["ts"] == "2026-09-30T12:00:00+00:00"
    assert data["payload"]["when"] == "2026-09-30"
    data["field_from_the_future"] = {"x": 1}
    back = RunEvent.from_dict(data)
    assert (back.event_id, back.type, back.seq, back.ts) == (event.event_id, event.type, 3, event.ts)
    assert back.payload["rows_written"] == 6


def test_a_naive_timestamp_is_read_as_utc_and_events_are_frozen():
    event = _event(ts=dt.datetime(2026, 9, 30, 12, 0))
    assert event.ts.tzinfo is dt.timezone.utc
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.seq = 5
    with pytest.raises(ValueError, match="missing"):
        RunEvent.from_dict({"type": "run.started"})
    with pytest.raises(ValueError, match="type"):
        _event(type_="")


def test_events_match_the_envelope_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = events.event_json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    for event in _run_events():
        validator.validate(event.to_dict())


# ---------------------------------------------------------------------- payload helpers


def test_redact_text_masks_secrets():
    text = ("connect postgresql+psycopg://ldp:hunter2@db:5432/x failed; token=abc123 "
            "Authorization: Bearer eyJhbGciOi AKIAIOSFODNN7EXAMPLE "
            "-----BEGIN PRIVATE KEY-----\nMIIEv\n-----END PRIVATE KEY----- password: 's3cret'")
    redacted = redact_text(text)
    for secret in ("hunter2", "abc123", "eyJhbGciOi", "AKIAIOSFODNN7EXAMPLE", "MIIEv", "s3cret"):
        assert secret not in redacted
    assert "postgresql+psycopg://ldp:***@db:5432/x" in redacted
    assert redact_text("x" * 5000).endswith("...") and len(redact_text("x" * 5000)) == 2000


def test_quality_payload_keeps_verdicts_and_counts_but_no_failing_values(sample_table):
    import pyarrow as pa

    table = pa.concat_tables([sample_table.slice(0, 2), sample_table.slice(0, 2)])
    report = run_checks(table, [Unique(["ride_id"]), AcceptedValues("city", ["LDN"]), Range("fare", min=0),
                                NotNull(["ride_id"]), Range("pickup_ts", min="2020-01-01")])
    payload = quality_payload(report)
    assert payload["checks_run"] == 5 and payload["checks_failed"] == 2 and payload["passed"] is False
    unique, accepted, fare, not_null, pickup = payload["results"]
    assert unique["column"] == "ride_id" and unique["failing_rows"] == 4
    assert "sample" not in unique["metrics"] and "e.g." not in unique["details"]
    assert "invalid_values" not in accepted["metrics"] and "BKK" not in accepted["details"]
    assert fare["metrics"]["observed_min"] == pytest.approx(11.5)
    assert "observed_min" not in pickup["metrics"], "bounds of a non-numeric column are data"
    assert not_null["passed"] is True
    json.dumps(payload)


def test_schema_payload_lists_names_and_types_with_nested_fields():
    import pyarrow as pa

    schema = pa.schema([("id", pa.int64()), ("pos", pa.struct([("x", pa.float64()), ("y", pa.float64())]))])
    assert schema_payload(schema) == [
        {"name": "id", "type": "int64"},
        {"name": "pos", "type": "struct<x: double, y: double>",
         "fields": [{"name": "x", "type": "double"}, {"name": "y", "type": "double"}]},
    ]


def test_dataset_ref_names_datasets_like_openlineage(tmp_path, catalog_config):
    from local_data_platform.format.csv import CSV
    from local_data_platform.format.iceberg import Iceberg

    iceberg = dataset_ref(Iceberg("rides", catalog_config))
    assert iceberg == {"namespace": (tmp_path / "warehouse").resolve().as_uri(), "name": "test_ns.rides",
                       "format": "ICEBERG", "table_identifier": "test_ns.rides"}
    csv = dataset_ref(CSV("rides", tmp_path / "rides.csv"))
    assert csv == {"namespace": "file", "name": (tmp_path / "rides.csv").resolve().as_posix(), "format": "CSV"}

    class ObjectStoreFile:
        name, format, path = "raw", "PARQUET", "s3://bucket/raw/2026/rides.parquet"

    assert dataset_ref(ObjectStoreFile()) == {"namespace": "s3://bucket", "name": "raw/2026/rides.parquet",
                                              "format": "PARQUET"}

    class Anything:
        name = "memory"

    assert dataset_ref(Anything()) == {"namespace": "ldp", "name": "memory", "format": None}


# ---------------------------------------------------------------------- simple sinks


def test_the_simple_sinks_satisfy_the_protocol():
    for sink in (NullSink(), MemorySink(), JsonlSink("x.jsonl"), MultiSink([]), OpenLineageSink("ol.jsonl"),
                 IcebergSink({"identifier": "ns", "warehouse_path": "wh"})):
        assert isinstance(sink, EventSink)


def test_memory_sink_keeps_events_in_order():
    sink = MemorySink()
    for event in _run_events():
        sink.emit(event)
    sink.flush()
    assert sink.types == ["run.started", "run.extracted", "quality.evaluated", "run.published", "run.finished"]
    assert sink.flushes == 1


def test_jsonl_sink_appends_one_event_per_line_and_reads_back(tmp_path):
    sink = JsonlSink(".ldp/events.jsonl", base_dir=tmp_path)
    sent = _run_events()
    for event in sent:
        sink.emit(event)
    path = tmp_path / ".ldp" / "events.jsonl"
    assert len(path.read_text().splitlines()) == len(sent)
    assert [e.event_id for e in read_jsonl(path)] == [e.event_id for e in sent]


def test_read_jsonl_skips_a_torn_line(tmp_path, caplog):
    path = tmp_path / "events.jsonl"
    good = _event()
    path.write_text(good.to_json() + "\n\n" + '{"event_id": "x", "ty' + "\n")
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        assert [e.event_id for e in read_jsonl(path)] == [good.event_id]
    assert "line 3" in caplog.text


def test_jsonl_sink_lines_do_not_interleave_across_threads(tmp_path):
    sink = JsonlSink(tmp_path / "events.jsonl")
    big = {"blob": "y" * 20_000}

    def writer(index):
        for seq in range(25):
            sink.emit(_event(run_id=f"run{index}", seq=seq, payload=big))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(read_jsonl(tmp_path / "events.jsonl")) == 100


class BrokenSink:
    def __init__(self):
        self.calls = 0

    def emit(self, event):
        self.calls += 1
        raise RuntimeError("sink is down, password=hunter2")

    def flush(self):
        raise RuntimeError("cannot flush")


def test_multi_sink_isolates_a_failing_sink(caplog):
    broken, memory = BrokenSink(), MemorySink()
    sink = MultiSink([broken, memory])
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        sink.emit(_event())
        sink.flush()
    assert broken.calls == 1 and len(memory.events) == 1 and memory.flushes == 1
    assert "sink is down" in caplog.text and "hunter2" not in caplog.text
    with pytest.raises(TypeError, match="emit"):
        MultiSink([object()])


def test_run_emitter_numbers_events_merges_context_and_swallows_sink_errors(caplog):
    memory = MemorySink()
    emitter = RunEmitter(memory, "r1", attempt=2, context={"pipeline": "p"}, clock=lambda: T0)
    emitter.emit("run.started", {"mode": "append"})
    emitter.emit("run.extracted", lambda: {"rows_read": 3}, table_uuid="u-1")
    emitter.emit("run.finished")
    assert [(e.seq, e.attempt, e.ts) for e in memory.events] == [(0, 2, T0), (1, 2, T0), (2, 2, T0)]
    assert memory.events[0].payload == {"pipeline": "p", "mode": "append"}
    assert [e.table_uuid for e in memory.events] == [None, "u-1", "u-1"]

    failing = RunEmitter(BrokenSink(), "r2")
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        failing.emit("run.started")
        failing.emit("run.extracted", lambda: 1 / 0)
        failing.flush()
    assert "failed to record run.started" in caplog.text and "Could not build" in caplog.text


def test_a_disabled_emitter_builds_no_payloads():
    emitter = RunEmitter(None, "r1")
    assert emitter.enabled is False
    assert emitter.emit("run.started", lambda: pytest.fail("payload built for a NullSink")) is None


# ---------------------------------------------------------------------- summaries


def test_summarize_runs_folds_events_per_attempt_newest_first():
    published, blocked = _run_events("a" * 8), _run_events("b" * 8, status="blocked_quality")
    later = [dataclasses.replace(e, ts=e.ts + dt.timedelta(minutes=5)) for e in blocked]
    summary = summarize_runs(published + later)
    assert [run["run_id"] for run in summary] == ["b" * 8, "a" * 8]
    assert summary[0]["status"] == "blocked_quality" and summary[0]["checks"] == "0/1"
    assert (summary[1]["status"], summary[1]["rows_written"], summary[1]["snapshot_id"]) == ("published", 6, 42)
    running = summarize_runs(_run_events("c" * 8)[:2])
    assert running[0]["status"] == "running"


# ---------------------------------------------------------------------- OpenLineage

# The parts of the OpenLineage 2-0-2 RunEvent schema a consumer relies on.
OPENLINEAGE_RUN_EVENT = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["eventTime", "producer", "schemaURL", "run", "job", "eventType"],
    "properties": {
        "eventType": {"enum": ["START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER"]},
        "eventTime": {"type": "string", "format": "date-time"},
        "producer": {"type": "string", "format": "uri"},
        "schemaURL": {"type": "string", "format": "uri"},
        "run": {"type": "object", "required": ["runId"], "properties": {
            "runId": {"type": "string", "format": "uuid"},
            "facets": {"type": "object", "additionalProperties": {"$ref": "#/$defs/facet"}}}},
        "job": {"type": "object", "required": ["namespace", "name"], "properties": {
            "facets": {"type": "object", "additionalProperties": {"$ref": "#/$defs/facet"}}}},
        "inputs": {"type": "array", "items": {"$ref": "#/$defs/dataset"}},
        "outputs": {"type": "array", "items": {"$ref": "#/$defs/dataset"}},
    },
    "$defs": {
        "facet": {"type": "object", "required": ["_producer", "_schemaURL"],
                  "properties": {"_producer": {"format": "uri"}, "_schemaURL": {"format": "uri"}}},
        "dataset": {"type": "object", "required": ["namespace", "name"], "properties": {
            "namespace": {"type": "string"}, "name": {"type": "string"},
            "facets": {"type": "object", "additionalProperties": {"$ref": "#/$defs/facet"}},
            "inputFacets": {"type": "object", "additionalProperties": {"$ref": "#/$defs/facet"}},
            "outputFacets": {"type": "object", "additionalProperties": {"$ref": "#/$defs/facet"}}}},
    },
}


def _validate_openlineage(lineage: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(OPENLINEAGE_RUN_EVENT, format_checker=jsonschema.FormatChecker()).validate(lineage)


def test_openlineage_sink_writes_start_and_complete_with_facets(tmp_path):
    sink = OpenLineageSink("lineage/ol.jsonl", base_dir=tmp_path, namespace="acme")
    run_id = uuid7()
    for event in _run_events(run_id, window=["2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00"]):
        sink.emit(event)
    start, complete = read_openlineage(tmp_path / "lineage" / "ol.jsonl")
    for lineage in (start, complete):
        _validate_openlineage(lineage)
        assert lineage["run"]["runId"] == run_id
        assert lineage["job"] == {"namespace": "acme", "name": "rides", "facets": lineage["job"]["facets"]}
        assert lineage["schemaURL"].startswith("https://openlineage.io/spec/2-0-2/OpenLineage.json")
    assert (start["eventType"], complete["eventType"]) == ("START", "COMPLETE")
    assert start["inputs"][0] == {"namespace": "file", "name": "/data/rides.csv", "facets": {}}
    assert start["run"]["facets"]["nominalTime"]["nominalStartTime"] == "2026-09-01T00:00:00+00:00"

    [source], [target] = complete["inputs"], complete["outputs"]
    assert [f["name"] for f in source["facets"]["schema"]["fields"]] == ["ride_id", "fare"]
    assert (target["namespace"], target["name"]) == ("file:///wh", "ns.rides")
    metrics = target["facets"]["dataQualityMetrics"]
    assert metrics["rowCount"] == 6 and metrics["columnMetrics"]["fare"] == {"nullCount": 1}
    assert target["facets"]["dataQualityAssertions"]["assertions"] == [
        {"assertion": "not_null(ride_id)", "success": True, "column": "ride_id"}]
    stats = target["outputFacets"]["outputStatistics"]
    assert stats["rowCount"] == 6 and stats["size"] == 1234 and stats["fileCount"] == 2
    assert stats["_schemaURL"].endswith("OutputStatisticsOutputDatasetFacet")
    assert complete["run"]["facets"]["ldp_run"]["snapshotId"] == 42


def test_openlineage_run_ids_are_canonical_uuids(tmp_path):
    sink = OpenLineageSink(tmp_path / "ol.jsonl")
    hex_id = uuid.UUID(uuid7()).hex
    sink.emit(_run_events(hex_id)[0])
    sink.emit(_run_events("not-a-uuid")[0])
    first, second = read_openlineage(tmp_path / "ol.jsonl")
    assert first["run"]["runId"] == str(uuid.UUID(hex_id))
    assert uuid.UUID(second["run"]["runId"]).version == 5


@pytest.mark.parametrize("status, message", [("blocked_quality", "blocked by failed quality checks: not_null"),
                                             ("failed", "ValueError: boom")])
def test_openlineage_sink_reports_blocked_and_failed_runs_as_fail(tmp_path, status, message):
    sink = OpenLineageSink(tmp_path / "ol.jsonl")
    for event in _run_events(status=status):
        sink.emit(event)
    _, final = read_openlineage(tmp_path / "ol.jsonl")
    _validate_openlineage(final)
    assert final["eventType"] == "FAIL"
    assert final["run"]["facets"]["errorMessage"]["message"].startswith(message)
    assert "outputStatistics" not in json.dumps(final)


class _LineageHandler(http.server.BaseHTTPRequestHandler):
    received: list = []
    status = 201

    def do_POST(self):  # noqa: N802 - http.server's naming
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((self.path, self.headers.get("Authorization"), json.loads(body)))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def lineage_server():
    _LineageHandler.received = []
    _LineageHandler.status = 201
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LineageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _LineageHandler
    server.shutdown()
    server.server_close()


def test_openlineage_sink_posts_to_an_http_endpoint_with_a_token_from_the_environment(lineage_server, monkeypatch):
    url, handler = lineage_server
    monkeypatch.setenv("LDP_TEST_OL_TOKEN", "t0ken-value")
    sink = OpenLineageSink(url, api_key_env="LDP_TEST_OL_TOKEN")
    assert "t0ken-value" not in repr(sink)
    for event in _run_events():
        sink.emit(event)
    assert [(path, auth, body["eventType"]) for path, auth, body in handler.received] == [
        ("/api/v1/lineage", "Bearer t0ken-value", "START"), ("/api/v1/lineage", "Bearer t0ken-value", "COMPLETE")]


def test_an_openlineage_http_error_is_raised_by_the_sink_and_logged_by_the_emitter(lineage_server, caplog):
    url, handler = lineage_server
    handler.status = 500
    sink = OpenLineageSink(url + "/custom/path")
    with pytest.raises(LDPError, match="HTTP 500"):
        sink.emit(_run_events()[0])
    emitter = RunEmitter(sink, "r1")
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        emitter.emit("run.started", {"pipeline": "p"})
    assert handler.received[0][0] == "/custom/path"
    assert "HTTP 500" in caplog.text


# ---------------------------------------------------------------------- Iceberg sink


def test_iceberg_sink_appends_batches_to_day_partitioned_ldp_tables(tmp_path, catalog_config):
    sink = IcebergSink(catalog_config)
    first, second = _run_events(), _run_events(status="blocked_quality")
    for event in first + second:
        sink.emit(event)
    assert not (tmp_path / "warehouse").exists(), "nothing is written before flush"
    sink.flush()
    sink.flush()  # an empty flush commits nothing

    catalog = sink.catalog
    runs = catalog.load_table("_ldp.runs")
    quality = catalog.load_table("_ldp.quality_results")
    assert len(runs.snapshots()) == 1 and len(quality.snapshots()) == 1
    for table in (runs, quality):
        [field] = table.spec().fields
        assert (field.name, str(field.transform)) == ("ts_day", "day")
        assert table.properties["ldp.managed"] == "true"

    rows = runs.scan().to_arrow()
    assert rows.num_rows == 10
    finished = [r for r in rows.to_pylist() if r["type"] == "run.finished"]
    assert sorted(r["status"] for r in finished) == ["blocked_quality", "published"]
    assert {r["pipeline"] for r in finished} == {"rides"} and {r["table_identifier"] for r in finished} == {"ns.rides"}
    checks = quality.scan().to_arrow().to_pylist()
    assert sorted(c["passed"] for c in checks) == [False, True]
    assert checks[0]["check_name"] == "not_null(ride_id)" and checks[0]["column_name"] == "ride_id"

    back = read_iceberg_events(catalog, run_id=first[0].run_id)
    assert [e.event_id for e in back] == [e.event_id for e in first]
    assert back[-1].payload["status"] == "published"
    assert read_iceberg_events(catalog, pipeline="other") == []
    assert len(read_iceberg_events(catalog, table="ns.rides")) == 10 and read_iceberg_events(catalog, table="x") == []


def test_iceberg_sink_flushes_on_its_own_when_the_batch_is_full(catalog_config):
    sink = IcebergSink(catalog_config, batch_size=5)
    for event in _run_events() + _run_events()[:2]:
        sink.emit(event)
    assert len(sink.catalog.load_table("_ldp.runs").snapshots()) == 1
    sink.flush()
    assert sink.catalog.load_table("_ldp.runs").scan().to_arrow().num_rows == 7


def test_a_failed_iceberg_flush_keeps_its_buffer_for_the_next_flush(catalog_config, monkeypatch):
    sink = IcebergSink(catalog_config)
    for event in _run_events():
        sink.emit(event)
    real_append = IcebergSink._append
    monkeypatch.setattr(IcebergSink, "_append", lambda self, identifier, rows: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        sink.flush()
    monkeypatch.setattr(IcebergSink, "_append", real_append)
    sink.flush()
    assert sink.catalog.load_table("_ldp.runs").scan().to_arrow().num_rows == 5


def test_iceberg_sink_retries_a_commit_conflict(catalog_config, monkeypatch):
    from pyiceberg.exceptions import CommitFailedException
    from pyiceberg.table import Table

    sink = IcebergSink(catalog_config)
    sink.emit(_event(payload={"pipeline": "p"}))
    real_append, calls = Table.append, []

    def flaky_append(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise CommitFailedException("main moved")
        return real_append(self, *args, **kwargs)

    monkeypatch.setattr(Table, "append", flaky_append)
    monkeypatch.setattr(events.time, "sleep", lambda seconds: None)
    sink.flush()
    assert len(calls) == 2
    assert sink.catalog.load_table("_ldp.runs").scan().to_arrow().num_rows == 1


def test_iceberg_sink_writes_mcp_audit_records_to_a_day_partitioned_ldp_audit_table(tmp_path, catalog_config):
    from local_data_platform.mcp_server.audit import AuditRecord

    sink = IcebergSink(catalog_config)
    ok = AuditRecord(tool="query", status="ok", duration_ms=12.5, sql="SELECT 1", rows=1, truncated=False,
                     arguments={"max_rows": 10})
    failed = AuditRecord(tool="sample_rows", status="error", duration_ms=1.0, table="ns.rides",
                         error="cannot read s3://key:pa55-not-real@bucket/x with token=abc123")
    sink.emit_audit(ok.to_dict())
    sink.emit_audit(failed.to_dict())
    assert not (tmp_path / "warehouse").exists(), "nothing is written before flush"
    sink.flush()
    sink.flush()

    audit = sink.catalog.load_table("_ldp.audit")
    assert sink.audit_identifier == "_ldp.audit" and len(audit.snapshots()) == 1
    [field] = audit.spec().fields
    assert (field.name, str(field.transform)) == ("ts_day", "day")
    rows = sorted(audit.scan().to_arrow().to_pylist(), key=lambda row: row["tool"])
    assert [(r["tool"], r["status"], r["rows"], r["table_identifier"]) for r in rows] == [
        ("query", "ok", 1, None), ("sample_rows", "error", None, "ns.rides")]
    assert rows[0]["sql"] == "SELECT 1" and json.loads(rows[0]["arguments"]) == {"max_rows": 10}
    assert rows[0]["event_id"] == ok.event_id and rows[0]["ts"].tzinfo is not None
    assert "pa55-not-real" not in rows[1]["error"] and "abc123" not in rows[1]["error"]
    assert not sink.catalog.table_exists("_ldp.runs"), "audit records never go to _ldp.runs"


def test_iceberg_sink_flushes_audit_records_when_the_batch_is_full(catalog_config):
    from local_data_platform.mcp_server.audit import AuditRecord

    sink = IcebergSink(catalog_config, batch_size=2)
    for tool in ("list_tables", "query", "table_history"):
        sink.emit_audit(AuditRecord(tool=tool, status="ok", duration_ms=1.0).to_dict())
    assert sink.catalog.load_table("_ldp.audit").scan().to_arrow().num_rows == 2
    sink.flush()
    assert sink.catalog.load_table("_ldp.audit").scan().to_arrow().num_rows == 3


def test_iceberg_sink_never_shows_its_catalog_spec():
    sink = IcebergSink({"type": "rest", "uri": "https://cat", "namespace": "ns", "token": "s3cr3t"})
    assert "s3cr3t" not in repr(sink) and "rest" in repr(sink)
    with pytest.raises(ConfigError, match="catalog"):
        IcebergSink()
    with pytest.raises(ConfigError, match="batch_size"):
        IcebergSink({"identifier": "x", "warehouse_path": "y"}, batch_size=0)


# ---------------------------------------------------------------------- config


ICEBERG_META = {
    "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
    "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
}


def test_sink_specs_merge_shared_options_and_default_the_iceberg_catalog():
    metadata = {**ICEBERG_META, "observability": {
        "sinks": ["iceberg", "jsonl", {"type": "openlineage", "url": "http://ol:5000"}, "none"],
        "jsonl": {"path": "logs/events.jsonl"},
        "openlineage": {"namespace": "acme"},
    }}
    assert sink_specs(metadata) == [
        {"type": "iceberg", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
        {"type": "jsonl", "path": "logs/events.jsonl"},
        {"type": "openlineage", "namespace": "acme", "url": "http://ol:5000"},
        {"type": "null"},
    ]
    assert sink_specs(ICEBERG_META) == []


@pytest.mark.parametrize("observability, message", [
    (["jsonl"], "must be an object"),
    ({"sinks": ["kafka"]}, "unknown sink type 'kafka'"),
    ({"sinks": [{"type": "jsonl", "file": "x"}]}, r"unknown options \['file'\]"),
    ({"sinks": ["jsonl"], "retention": 3}, "unknown keys"),
    ({"sinks": [{"type": "openlineage", "path": "a", "url": "http://b"}]}, "not both"),
    ({"sinks": "jsonl", "jsonl": "x"}, "must be an object of sink options"),
])
def test_invalid_observability_blocks_are_config_errors(observability, message):
    with pytest.raises(ConfigError, match=message):
        sink_specs({**ICEBERG_META, "observability": observability})


def test_the_iceberg_sink_needs_a_catalog_when_no_table_is_iceberg():
    metadata = {"source": {"name": "a", "format": "CSV", "path": "a.csv"},
                "target": {"name": "b", "format": "PARQUET", "path": "b.parquet"},
                "observability": {"sinks": ["iceberg"]}}
    with pytest.raises(ConfigError, match="needs a 'catalog'"):
        sink_specs(metadata)
    metadata["observability"]["iceberg"] = {"catalog": {"identifier": "ops", "warehouse_path": "wh"}}
    assert sink_specs(metadata)[0]["catalog"] == {"identifier": "ops", "warehouse_path": "wh"}


def test_sink_from_config_builds_the_sinks_without_touching_disk(tmp_path):
    from local_data_platform import Config

    config = Config.from_dict({"identifier": "rides", "metadata": {
        **ICEBERG_META, "observability": {"sinks": ["iceberg", "jsonl"]}}}, base_dir=tmp_path)
    sink = sink_from_config(config)
    assert isinstance(sink, MultiSink)
    assert [type(s) for s in sink.sinks] == [IcebergSink, JsonlSink]
    assert sink.sinks[1].path == tmp_path / ".ldp" / "events.jsonl"
    assert list(tmp_path.iterdir()) == []
    single = Config.from_dict({"identifier": "x", "metadata": {**ICEBERG_META, "observability": {"sinks": ["jsonl"]}}},
                              base_dir=tmp_path)
    assert isinstance(sink_from_config(single), JsonlSink)
    assert isinstance(sink_from_config(Config.from_dict({"identifier": "x", "metadata": ICEBERG_META})), NullSink)
    assert isinstance(sink_from_config(None), NullSink)


# ---------------------------------------------------------------------- ldp runs


def _cli(argv):
    parser = argparse.ArgumentParser(prog="ldp")
    events.add_cli(parser.add_subparsers())
    args = parser.parse_args(argv)
    return args.handler(args)


def _config_file(folder: Path, sinks) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "rides.json"
    metadata = dict(ICEBERG_META)
    if sinks is not None:
        metadata["observability"] = {"sinks": sinks}
    path.write_text(json.dumps({"identifier": "rides", "metadata": metadata}))
    return path


def test_ldp_runs_reads_the_ldp_runs_table_of_a_config(tmp_path, capsys):
    path = _config_file(tmp_path, ["iceberg"])
    sink = IcebergSink(ICEBERG_META["target"]["catalog"], tmp_path)
    run = _run_events()
    for event in run + _run_events(status="failed"):
        sink.emit(event)
    sink.flush()

    assert _cli(["runs", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Runs recorded in _ldp.runs" in out and "published" in out and "failed" in out
    assert _cli(["runs", str(path), "--run", run[0].run_id]) == 0
    out = capsys.readouterr().out
    assert "run.started" in out and "status=published" in out and "(5 rows)" in out


def test_ldp_runs_reads_a_jsonl_file_and_a_config_jsonl_sink(tmp_path, capsys):
    events_file = tmp_path / ".ldp" / "events.jsonl"
    sink = JsonlSink(events_file)
    for event in _run_events() + _run_events(status="blocked_quality"):
        sink.emit(event)
    assert _cli(["runs", str(events_file), "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "blocked_quality" in out and "(1 row)" in out

    path = _config_file(tmp_path, ["jsonl"])
    loaded, where = load_run_events(path)
    assert where == str(events_file) and len(loaded) == 10
    assert _cli(["runs", str(path), "--events", "--limit", "3"]) == 0
    assert "(3 rows)" in capsys.readouterr().out


def test_ldp_runs_creates_nothing_when_nothing_was_recorded(tmp_path, capsys):
    path = _config_file(tmp_path / "project", None)
    assert _cli(["runs", str(path)]) == 0
    assert "No runs recorded" in capsys.readouterr().out
    assert sorted(p.name for p in (tmp_path / "project").iterdir()) == ["rides.json"]


# ---------------------------------------------------------------------- docs


def test_the_python_blocks_in_docs_observability_run_in_order(tmp_path):
    import re
    import subprocess
    import sys

    pytest.importorskip("duckdb")
    doc = Path(__file__).resolve().parents[1] / "docs" / "observability.md"
    blocks = re.findall(r"```python\n(.*?)```", doc.read_text(), flags=re.DOTALL)
    assert len(blocks) == 3
    (tmp_path / "rides.csv").write_text("ride_id,fare\n1,12.5\n2,8.0\n")
    (tmp_path / "rides.json").write_text(json.dumps({"identifier": "rides_daily", "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides_daily", "format": "ICEBERG",
                   "catalog": {"identifier": "nyc", "warehouse_path": "warehouse"}}}}))
    script = tmp_path / "doc.py"
    script.write_text("namespace = {}\n" + "\n".join(
        f"exec(compile({block!r}, 'block {i}', 'exec'), namespace)" for i, block in enumerate(blocks, start=1)))
    done = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == "True"
    assert lines[1] == str(["run.started", "run.extracted", "quality.evaluated", "run.published", "run.finished"])
    assert lines[2] == "run.finished"
    assert lines[3] == "published 3 1/1"
    assert lines[4] == "[{'check_name': 'not_null(ride_id)', 'passed': True}]"
