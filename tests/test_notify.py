"""The notifier: the channels of a config, the senders, the alerts of a run and ``ldp alert test``."""

import copy
import dataclasses
import datetime as dt
import json
import logging
import re
from pathlib import Path

import pytest

from local_data_platform import Config
from local_data_platform.cli import main
from local_data_platform.events import (
    MultiSink,
    NullSink,
    RunEmitter,
    RunEvent,
    TaggedSink,
    redact_text,
    sink_from_config,
    sink_specs,
)
from local_data_platform.exceptions import ConfigError
from local_data_platform.notify import (
    ALERT_SCHEMA,
    ALERT_VERSION,
    DEFAULT_FILE,
    DEFAULT_ON,
    EVENTS,
    TIMEOUT_S,
    Alert,
    Channel,
    Delivery,
    NotifySink,
    Notifier,
    alert_json_schema,
    alert_problems,
    alerts_schema,
    command_of,
    deliver,
    notifier_for,
    parse_alerts,
)
from local_data_platform.spec import json_schema, validate_spec

UTC = dt.timezone.utc
AT = dt.datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
SLACK = {"type": "slack", "url_env": "LDP_SLACK_WEBHOOK"}
WEBHOOK = {"type": "webhook", "url_env": "ALERT_URL", "token_env": "ALERT_TOKEN"}
FILE = {"type": "file", "path": "alerts/out.jsonl"}
SECRET_PATH = "/services/T0001/B0002/XXSECRETXXTOKEN"


def _spec(alerts, **extra) -> dict:
    metadata = {"source": {"name": "orders", "format": "CSV", "path": "orders.csv"},
                "target": {"name": "orders", "format": "ICEBERG", "write_mode": "append",
                           "catalog": {"identifier": "shop", "warehouse_path": "warehouse"}},
                "observability": {"alerts": alerts}, **extra}
    return {"identifier": "orders", "who": "ops team", "metadata": metadata}


def _messages(spec) -> list[str]:
    return [str(error) for error in validate_spec(spec)]


def _notifier(*channels: dict, tmp_path: Path | None = None, **context) -> Notifier:
    parsed = parse_alerts({"observability": {"alerts": list(channels)}})
    fields = {"pipeline": "orders", "table": "shop.orders", "owner": "ops team",
              "command": "ldp status config/orders.json", "base_dir": tmp_path, **context}
    return Notifier(channels=parsed, **fields)


def _monitor_result(**changes):
    from local_data_platform.monitoring.results import MonitorResult

    values = {"monitor": "freshness", "kind": "freshness", "status": "error", "evaluated_at": AT, "pipeline": "orders",
              "table_identifier": "shop.orders", "observed": "30.1 hour", "expected": "error after 26 hour",
              "details": "the last commit was 30.1 hour ago, at 2026-10-27T05:54:00Z"}
    return MonitorResult(**{**values, **changes})


@pytest.fixture
def channels_env(lineage_server, monkeypatch):
    """The environment of a Slack and a webhook channel that point at the local server. Yields ``(url, handler)``."""
    url, handler = lineage_server
    monkeypatch.setenv("LDP_SLACK_WEBHOOK", url + "/slack" + SECRET_PATH)
    monkeypatch.setenv("ALERT_URL", url + "/hook")
    monkeypatch.setenv("ALERT_TOKEN", "t0ken-value")
    handler.status = 200
    return url, handler


# ---------------------------------------------------------------------- the channels of a config


def test_channels_are_read_with_their_defaults():
    spec = _spec([SLACK, {**WEBHOOK, "on": ["monitor.error"], "repeat_after_minutes": 60}, {"type": "file"}])
    channels = parse_alerts(spec["metadata"])
    assert channels == (
        Channel(kind="slack", url_env="LDP_SLACK_WEBHOOK"),
        Channel(kind="webhook", url_env="ALERT_URL", token_env="ALERT_TOKEN", on=("monitor.error",),
                repeat_after_minutes=60),
        Channel(kind="file"))
    assert channels[0].on == DEFAULT_ON == ("run.failed", "run.blocked_quality", "monitor.error", "monitor.resolved")
    assert [channel.label for channel in channels] == ["slack LDP_SLACK_WEBHOOK", "webhook ALERT_URL",
                                                       f"file {DEFAULT_FILE}"]
    assert Channel(kind="file", path="a/b.jsonl").label == "file a/b.jsonl"
    assert channels[0].wants("run.failed") and not channels[0].wants("monitor.heartbeat")


def test_a_config_with_no_alerts_has_no_channel_and_no_notifier(tmp_path):
    assert parse_alerts({"source": {}, "target": {}}) == ()
    assert parse_alerts({"observability": {"sinks": ["jsonl"]}}) == ()
    assert parse_alerts({"observability": {"alerts": []}}) == ()
    spec = _spec([])
    config = Config.from_dict(spec, base_dir=tmp_path)
    assert notifier_for(config) is None


GOOD = [
    [SLACK], [WEBHOOK], [FILE], [{"type": "file"}], [SLACK, WEBHOOK, FILE],
    [{**SLACK, "on": list(EVENTS)}], [{**WEBHOOK, "repeat_after_minutes": 1}], [{**FILE, "on": ["monitor.heartbeat"]}],
    [{"type": "slack", "url_env": "_x1"}],
]


@pytest.mark.parametrize("alerts", GOOD)
def test_these_lists_are_fine(alerts):
    assert alert_problems(alerts) == []
    assert _messages(_spec(alerts)) == []


PROBLEMS = [
    ("hooks", [("", "must be a list of channels such as {'type': 'slack', 'url_env': 'LDP_SLACK_WEBHOOK'}, "
                    "got 'hooks'")]),
    ({"type": "slack"}, [("", "must be a list of channels such as {'type': 'slack', 'url_env': 'LDP_SLACK_WEBHOOK'}, "
                              "got {'type': 'slack'}")]),
    (["slack"], [("[0]", "must be an object with a type, such as {'type': 'slack', 'url_env': 'LDP_SLACK_WEBHOOK'}, "
                         "got 'slack'")]),
    ([{"url_env": "X"}], [("[0].type", "is required, one of ['slack', 'webhook', 'file']")]),
    ([{"type": "pager"}], [("[0].type", "must be one of ['slack', 'webhook', 'file'], got 'pager'")]),
    ([{"type": ["slack"]}], [("[0].type", "must be one of ['slack', 'webhook', 'file'], got ['slack']")]),
    ([{"type": "slack"}], [("[0].url_env", "is required, the name of the environment variable with the URL")]),
    ([{"type": "webhook"}], [("[0].url_env", "is required, the name of the environment variable with the URL")]),
    ([{"type": "slack", "url_env": "my-url"}],
     [("[0].url_env", "must be the name of an environment variable such as LDP_SLACK_WEBHOOK, got 'my-url'")]),
    ([{"type": "slack", "url_env": "1ABC"}],
     [("[0].url_env", "must be the name of an environment variable such as LDP_SLACK_WEBHOOK, got '1ABC'")]),
    ([{"type": "slack", "url_env": 7}],
     [("[0].url_env", "must be the name of an environment variable such as LDP_SLACK_WEBHOOK, got 7")]),
    ([{"type": "slack", "url_env": "https://hooks.slack.com/services/T0/B0/XXXX"}],
     [("[0].url_env", "is a URL, and it has to be the name of an environment variable such as LDP_SLACK_WEBHOOK, so "
                      "the URL stays out of the config")]),
    ([{"type": "webhook", "url_env": "ALERT_URL", "token_env": "tok en"}],
     [("[0].token_env", "must be the name of an environment variable such as ALERT_TOKEN, got 'tok en'")]),
    ([{"type": "webhook", "url_env": "ALERT_URL", "token_env": "https://x.example/t"}],
     [("[0].token_env", "is a URL, and it has to be the name of an environment variable such as ALERT_TOKEN, so the "
                        "URL stays out of the config")]),
    ([{**SLACK, "token_env": "T"}],
     [("[0]", "has unknown keys ['token_env']; allowed: ['type', 'url_env', 'on', 'repeat_after_minutes']")]),
    ([{**FILE, "url_env": "U"}], [("[0]", "has unknown keys ['url_env']; allowed: ['type', 'path', 'on', "
                                          "'repeat_after_minutes']")]),
    ([{"type": "file", "path": ""}], [("[0].path", "must be a file name, got ''")]),
    ([{"type": "file", "path": 3}], [("[0].path", "must be a file name, got 3")]),
    ([{**SLACK, "on": []}], [("[0].on", f"must be a list of at least one of {list(EVENTS)}, got []")]),
    ([{**SLACK, "on": "monitor.error"}],
     [("[0].on", f"must be a list of at least one of {list(EVENTS)}, got 'monitor.error'")]),
    ([{**SLACK, "on": ["monitor.error", "run.crashed"]}],
     [("[0].on[1]", f"must be one of {list(EVENTS)}, got 'run.crashed'")]),
    ([{**SLACK, "on": ["monitor.error", "monitor.error"]}], [("[0].on", "lists an event twice")]),
    ([{**SLACK, "repeat_after_minutes": 0}],
     [("[0].repeat_after_minutes", "must be a whole number of minutes from 1, got 0")]),
    ([{**SLACK, "repeat_after_minutes": True}],
     [("[0].repeat_after_minutes", "must be a whole number of minutes from 1, got True")]),
    ([{**SLACK, "repeat_after_minutes": "5"}],
     [("[0].repeat_after_minutes", "must be a whole number of minutes from 1, got '5'")]),
    ([{**SLACK, "repeat_after_minutes": 2.5}],
     [("[0].repeat_after_minutes", "must be a whole number of minutes from 1, got 2.5")]),
    ([SLACK, {"type": "pager"}, {"type": "file", "on": []}],
     [("[1].type", "must be one of ['slack', 'webhook', 'file'], got 'pager'"),
      ("[2].on", f"must be a list of at least one of {list(EVENTS)}, got []")]),
]


@pytest.mark.parametrize("alerts, expected", PROBLEMS)
def test_every_malformed_channel_is_reported_with_its_path(alerts, expected):
    assert alert_problems(alerts) == expected
    first_path, first_message = expected[0]
    with pytest.raises(ConfigError) as caught:
        parse_alerts(_spec(alerts)["metadata"])
    assert str(caught.value) == f"metadata.observability.alerts{first_path}: {first_message}"
    assert _messages(_spec(alerts))[0].startswith(f"metadata.observability.alerts{first_path}: ")


def test_a_url_in_url_env_is_never_repeated_in_a_message():
    problems = alert_problems([{"type": "slack", "url_env": "https://hooks.slack.com/services/T0/B0/XXSECRETXX"},
                               {"type": "webhook", "url_env": "https://hc-ping.example/abcdef-uuid"},
                               {"type": "webhook", "url_env": "http://10.0.0.5:8080/hook-uuid-0042"}])
    assert "XXSECRETXX" not in str(problems) and "abcdef-uuid" not in str(problems)
    assert "hook-uuid-0042" not in str(problems)
    assert all(message.startswith("is a URL, and it has to be the name of an environment variable")
               for _, message in problems)


def test_a_value_that_is_not_a_name_is_repeated_with_a_slack_path_masked():
    value = "not a name https://hooks.slack.com/services/T0/B0/XXX"
    message = dict(alert_problems([{"type": "slack", "url_env": value}]))
    assert "XXX" not in message["[0].url_env"]


def test_a_value_that_is_not_a_name_and_not_a_url_is_repeated_masked_and_cut():
    value = "my key AKIAIOSFODNN7EXAMPLE " + "z" * 300
    [(path, message)] = alert_problems([{"type": "slack", "url_env": value}])
    assert path == "[0].url_env" and message.startswith("must be the name of an environment variable such as ")
    assert "AKIAIOSFODNN7EXAMPLE" not in message and "z" * 100 not in message and message.endswith("...")


def test_the_sink_options_accept_alerts_and_the_spec_hash_ignores_them():
    from local_data_platform.spec import spec_hash

    assert sink_specs(_spec([SLACK])["metadata"]) == []
    assert spec_hash(_spec([SLACK])) == spec_hash(_spec([SLACK, WEBHOOK])) == spec_hash(_spec([]))


def test_the_json_schema_describes_the_alerts_and_agrees_with_validate_spec():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json_schema()
    assert schema["$defs"]["observability"]["properties"]["alerts"] == alerts_schema()
    validator = jsonschema.Draft202012Validator(schema)
    for good in GOOD:
        validator.validate(_spec(good))
    bad = [[{"url_env": "X"}], [{"type": "pager"}], [{"type": "slack"}], [{"type": "slack", "url_env": "a b"}],
           [{"type": "slack", "url_env": "https://x"}], [{**SLACK, "token_env": "T"}], [{**SLACK, "on": []}],
           [{**SLACK, "on": ["monitor.error", "monitor.error"]}], [{**SLACK, "on": ["nope"]}],
           [{**SLACK, "repeat_after_minutes": 0}], [{"type": "file", "path": ""}], "slack", [5], [{}],
           [{"path": "a.jsonl"}], [{"type": "webhook"}], [{"type": "webhook", "token_env": "T"}]]
    for alerts in bad:
        assert list(validator.iter_errors(_spec(alerts))), alerts
        assert _messages(_spec(alerts)), alerts


def test_the_schema_of_a_channel_says_what_it_needs_and_what_it_hears_by_default():
    slack, webhook, file = alerts_schema()["items"]["oneOf"]
    for channel in (slack, webhook, file):
        assert channel["properties"]["on"]["default"] == list(DEFAULT_ON)
        assert channel["properties"]["on"]["items"] == {"enum": list(EVENTS)}
    assert (slack["required"], webhook["required"], file["required"]) == (
        ["type", "url_env"], ["type", "url_env"], ["type"])
    assert file["properties"]["path"]["default"] == DEFAULT_FILE


def test_ldp_schema_has_the_alerts(capsys):
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    items = schema["$defs"]["observability"]["properties"]["alerts"]["items"]["oneOf"]
    assert [item["properties"]["type"]["const"] for item in items] == ["slack", "webhook", "file"]
    assert items[2]["properties"]["path"]["default"] == DEFAULT_FILE


# ---------------------------------------------------------------------- secrets


@pytest.mark.parametrize("url", [
    "https://hooks.slack.com/services/T0001/B0002/XXSECRETXX", "https://hooks.slack.com/workflows/T0/A1/12/abc",
    "https://hooks.slack.com/triggers/T0/12/abc"])
def test_redact_text_masks_the_path_of_a_slack_webhook(url):
    masked = redact_text(f"could not post to {url} (connection reset), then went on")
    head = url.split("/")[:4]
    assert masked == f"could not post to {'/'.join(head)}/*** (connection reset), then went on"
    assert redact_text("https://hooks.slack.com/services/T0/B0/XXX") == "https://hooks.slack.com/services/***"
    assert redact_text("see https://example.com/services/T0/B0/XXX") == "see https://example.com/services/T0/B0/XXX"


@pytest.mark.parametrize("end", [" ", ")", '"', "'", "<", ">"])
def test_redact_text_ends_the_masked_slack_path_at_a_space_a_quote_or_a_bracket(end):
    url = "https://hooks.slack.com/services/T0/B0/XXX"
    assert redact_text(f"a{end}{url}{end}b") == f"a{end}https://hooks.slack.com/services/***{end}b"


def test_a_config_with_a_slack_webhook_url_anywhere_is_refused():
    url = "https://hooks.slack.com/services/T0001/B0002/XXSECRETXX"
    in_text, in_how = _spec([SLACK]), _spec([SLACK])
    in_text["what"], in_how["how"] = f"post to {url}", url
    for spec in (in_text, _spec([{"type": "slack", "url_env": url}]), in_how):
        messages = _messages(spec)
        assert any("looks like an inline secret" in message for message in messages), messages
        assert "XXSECRETXX" not in " ".join(messages)


# ---------------------------------------------------------------------- an alert


def test_an_alert_is_the_ldp_alert_schema_without_the_keys_it_has_no_value_for():
    alert = Alert("monitor.error", AT, pipeline="orders", table="shop.orders", owner="ops team",
                  command="ldp status config/orders.json", facts={"monitor": "freshness", "status": "error"})
    assert alert.to_dict() == {
        "schema": "ldp.alert", "version": 1, "event": "monitor.error", "at": "2026-10-28T12:00:00Z",
        "pipeline": "orders", "table": "shop.orders", "owner": "ops team", "command": "ldp status config/orders.json",
        "monitor": "freshness", "status": "error"}
    assert (ALERT_SCHEMA, ALERT_VERSION) == ("ldp.alert", 1)
    assert Alert("alert.test", AT).to_dict() == {
        "schema": "ldp.alert", "version": 1, "event": "alert.test", "at": "2026-10-28T12:00:00Z"}


def test_an_alert_of_an_event_that_has_no_headline_says_the_name_of_the_event():
    assert Alert("run.retried", AT, pipeline="orders").text() == "ldp run.retried on orders"


def test_an_alert_moment_is_utc_whatever_the_zone_it_was_made_in():
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert Alert("alert.test", dt.datetime(2026, 10, 28, 17, 30, 45, 123456, tzinfo=tz)).to_dict()["at"] == (
        "2026-10-28T12:00:45Z")


def test_the_text_of_a_monitor_alert_names_everything_that_a_person_needs():
    notifier = _notifier(SLACK)
    error = notifier.monitor_alert(_monitor_result(), "monitor.error", "ok")
    assert error.text() == (
        "ldp monitor error on orders (table shop.orders, owner ops team)\n"
        "freshness: observed 30.1 hour, expected error after 26 hour\n"
        "the last commit was 30.1 hour ago, at 2026-10-27T05:54:00Z\n"
        "Look closer with `ldp status config/orders.json`")
    again = notifier.monitor_alert(_monitor_result(), "monitor.error", "error", repeat=True)
    assert again.text().splitlines()[0] == "ldp monitor error again on orders (table shop.orders, owner ops team)"
    assert again.to_dict()["repeat"] is True and "repeat" not in error.to_dict()
    assert error.to_dict()["at"] == error.to_dict()["evaluated_at"] == "2026-10-28T12:00:00Z"
    warn = notifier.monitor_alert(_monitor_result(status="warn"), "monitor.warn", "ok")
    assert warn.text().splitlines()[0] == "ldp monitor warning on orders (table shop.orders, owner ops team)"
    resolved = notifier.monitor_alert(_monitor_result(status="ok", observed="2.0 hour"), "monitor.resolved", "error")
    assert resolved.text().splitlines()[:2] == [
        "ldp monitor resolved on orders (table shop.orders, owner ops team)", "freshness is ok again, it was error"]
    assert resolved.to_dict()["previous_status"] == "error" and resolved.to_dict()["status"] == "ok"


def test_the_text_of_a_run_alert_has_the_stage_the_kind_of_error_and_the_failed_checks():
    notifier = _notifier(SLACK)
    ts = AT
    failed = RunEvent(event_id="e1", type="run.failed", schema_version=1, ts=ts, run_id="r1", attempt=2, seq=3,
                      payload={"pipeline": "orders", "table": "shop.orders", "stage": "extract",
                               "error_type": "ConnectionError", "message": "boom"})
    assert notifier.run_alert(failed).text() == (
        "ldp run failed on orders (table shop.orders, owner ops team)\n"
        "stage extract, ConnectionError, attempt 2\n"
        "Look closer with `ldp status config/orders.json`")
    details = {"pipeline": "orders", "table": "shop.orders", "checks_failed": 2,
               "failures": ["not_null(amount)", "unique(order_id)"]}
    blocked = RunEvent(event_id="e2", type="run.blocked_quality", schema_version=1, ts=ts, run_id="r1", attempt=1,
                       seq=3, payload=details)
    assert notifier.run_alert(blocked).text().splitlines()[:2] == [
        "ldp run blocked by a quality check on orders (table shop.orders, owner ops team)",
        "2 checks failed: not_null(amount), unique(order_id)"]
    one = dataclasses.replace(
        blocked, payload={**blocked.payload, "checks_failed": 1, "failures": ["not_null(amount)"]})
    assert notifier.run_alert(one).text().splitlines()[1] == "1 check failed: not_null(amount)"
    nameless = dataclasses.replace(blocked, payload={**blocked.payload, "checks_failed": 3, "failures": []})
    assert notifier.run_alert(nameless).text().splitlines()[1] == "3 checks failed"


def test_an_alert_keeps_a_value_that_is_zero_or_empty_and_leaves_out_only_what_is_none():
    notifier = _notifier(SLACK)
    heartbeat = notifier.heartbeat_alert([], AT).to_dict()
    assert heartbeat["monitors"] == 0 and heartbeat["results"] == {}
    event = RunEvent(event_id="e", type="run.blocked_quality", schema_version=1, ts=AT, run_id="r", attempt=1, seq=1,
                     payload={"pipeline": "orders", "checks_failed": 0, "failures": []})
    body = notifier.run_alert(event).to_dict()
    assert body["checks_failed"] == 0 and body["failures"] == []


def test_a_monitor_alert_with_nothing_observed_or_expected_says_a_dash_and_has_no_line_of_details():
    result = _monitor_result(observed=None, expected=None, details="")
    lines = _notifier(SLACK).monitor_alert(result, "monitor.error", None).text().splitlines()
    assert lines == ["ldp monitor error on orders (table shop.orders, owner ops team)",
                     "freshness: observed -, expected -", "Look closer with `ldp status config/orders.json`"]


def test_the_text_of_a_heartbeat_and_a_test_message():
    notifier = _notifier(SLACK)
    heartbeat = notifier.heartbeat_alert([_monitor_result(status="ok"), _monitor_result(status="ok"),
                                          _monitor_result(status="error")], AT)
    assert heartbeat.text().splitlines()[:2] == ["ldp monitor ran on orders (table shop.orders, owner ops team)",
                                                 "results: 2 ok, 1 error"]
    assert heartbeat.to_dict()["monitors"] == 3 and heartbeat.to_dict()["results"] == {"ok": 2, "error": 1}
    assert notifier.heartbeat_alert([], AT).text().splitlines()[1] == "results: none"


def test_an_alert_with_no_command_or_owner_says_less_and_not_none():
    notifier = Notifier(channels=(), pipeline="orders")
    text = notifier.alert("monitor.error", AT, monitor="freshness", observed="1", expected="2").text()
    assert text == "ldp monitor error on orders\nfreshness: observed 1, expected 2"
    assert Notifier(channels=()).alert("alert.test", AT, message="hello").text() == "ldp alert test\nhello"


def test_every_alert_is_valid_against_the_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(alert_json_schema())
    notifier = _notifier(SLACK)
    run = RunEvent(event_id="e", type="run.failed", schema_version=1, ts=AT, run_id="r", attempt=1, seq=1,
                   payload={"pipeline": "orders", "stage": "extract", "error_type": "OSError"})
    alerts = [notifier.monitor_alert(_monitor_result(), "monitor.error", None),
              notifier.monitor_alert(_monitor_result(status="ok"), "monitor.resolved", "error", repeat=False),
              notifier.heartbeat_alert([_monitor_result()], AT), notifier.run_alert(run),
              notifier.alert("alert.test", AT, message="hi")]
    for alert in alerts:
        validator.validate(json.loads(json.dumps(alert.to_dict())))
    assert list(validator.iter_errors({"schema": "other", "version": 1, "event": "alert.test", "at": "x"}))
    assert list(validator.iter_errors({"schema": "ldp.alert", "version": 2, "event": "alert.test",
                                       "at": "2026-10-28T12:00:00Z"}))
    assert list(validator.iter_errors({"schema": "ldp.alert", "version": 1, "event": "other", "at": "x"}))
    assert list(validator.iter_errors({"schema": "ldp.alert", "version": 1, "event": "alert.test"}))


def test_the_schema_of_an_alert_says_the_type_of_each_key_and_which_keys_are_required():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(alert_json_schema())
    base = {"schema": "ldp.alert", "version": 1, "event": "monitor.error", "at": "2026-10-28T12:00:00Z"}
    validator.validate(base)
    for required in base:
        assert list(validator.iter_errors({key: value for key, value in base.items() if key != required})), required
    fine = {"previous_status": "warn", "status": "unknown", "repeat": True, "checks_failed": 2, "attempt": 1,
            "monitors": 3, "results": {"ok": 2}, "failures": ["not_null(amount)"], "pipeline": "orders"}
    for key, value in fine.items():
        validator.validate({**base, key: value})
    wrong = {"previous_status": "unknown", "status": "broken", "repeat": "yes", "checks_failed": "2", "attempt": "1",
             "monitors": "3", "results": {"ok": "2"}, "failures": [1], "pipeline": 5}
    for key, value in wrong.items():
        assert list(validator.iter_errors({**base, key: value})), key


def test_the_schema_of_an_alert_allows_keys_that_a_later_version_adds():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(alert_json_schema())
    validator.validate({"schema": "ldp.alert", "version": 1, "event": "alert.test", "at": "2026-10-28T12:00:00Z",
                        "something_new": [1, 2]})


# ---------------------------------------------------------------------- what an alert never holds


def test_a_run_alert_never_holds_the_message_of_an_error_or_a_value_of_a_row():
    notifier = _notifier(SLACK)
    event = RunEvent(event_id="e", type="run.failed", schema_version=1, ts=AT, run_id="r", attempt=1, seq=1,
                     payload={"pipeline": "orders", "table": "shop.orders", "stage": "transform",
                              "error_type": "ArrowInvalid",
                              "message": "Could not convert 'Pune-secret-city' with token=abc123 to int64"})
    alert = notifier.run_alert(event)
    everything = json.dumps(alert.to_dict()) + alert.text()
    assert "Pune-secret-city" not in everything and "abc123" not in everything and "Could not convert" not in everything
    assert alert.to_dict()["error_type"] == "ArrowInvalid" and alert.to_dict()["stage"] == "transform"


def test_a_blocked_run_alert_lists_the_names_of_the_failed_checks_and_at_most_10():
    names = [f"not_null(c{number})" for number in range(14)]
    event = RunEvent(event_id="e", type="run.blocked_quality", schema_version=1, ts=AT, run_id="r", attempt=1, seq=1,
                     payload={"pipeline": "orders", "checks_failed": 14, "failures": names,
                              "contract_problems": ["column secret_value 'x' is wrong"], "on_failure": "fail"})
    body = _notifier(SLACK).run_alert(event).to_dict()
    assert body["failures"] == names[:10] and body["checks_failed"] == 14
    assert "contract_problems" not in body and "secret_value" not in json.dumps(body)


def test_the_text_that_a_monitor_alert_carries_is_redacted_and_cut():
    notifier = _notifier(SLACK)
    details = "failed with token=abc123 at https://hooks.slack.com/services/T/B/XX"
    alert = notifier.monitor_alert(_monitor_result(details=details, observed="x" * 600), "monitor.error", None)
    body = alert.to_dict()
    assert "abc123" not in body["details"] and body["details"].endswith("https://hooks.slack.com/services/***")
    assert len(body["observed"]) == 200 and body["observed"].endswith("...")


LONG = "AKIAIOSFODNN7EXAMPLE " + "y" * 700


@pytest.mark.parametrize("key, limit", [("monitor", 200), ("kind", 100), ("observed", 200), ("expected", 200),
                                        ("details", 500)])
def test_each_text_of_a_monitor_alert_is_masked_and_cut_to_its_own_limit(key, limit):
    body = _notifier(SLACK).monitor_alert(_monitor_result(**{key: LONG}), "monitor.error", None).to_dict()
    assert "AKIAIOSFODNN7EXAMPLE" not in body[key] and len(body[key]) == limit and body[key].endswith("...")


@pytest.mark.parametrize("key, limit", [("stage", 100), ("error_type", 100), ("workflow", 200), ("task_id", 200),
                                        ("pipeline", 200), ("table", 200)])
def test_each_text_of_a_run_alert_is_masked_and_cut_to_its_own_limit(key, limit):
    payload = {"pipeline": "orders", "table": "shop.orders", "stage": "x", "error_type": "E", "workflow": "w",
               "task_id": "t", key: LONG}
    event = RunEvent(event_id="e", type="run.failed", schema_version=1, ts=AT, run_id="r", attempt=1, seq=1,
                     payload=payload)
    body = _notifier(SLACK).run_alert(event).to_dict()
    assert "AKIAIOSFODNN7EXAMPLE" not in body[key] and len(body[key]) == limit and body[key].endswith("...")


def test_each_name_of_a_failed_check_is_masked_and_cut_to_100():
    event = RunEvent(event_id="e", type="run.blocked_quality", schema_version=1, ts=AT, run_id="r", attempt=1, seq=1,
                     payload={"pipeline": "orders", "checks_failed": 1, "failures": [LONG]})
    [name] = _notifier(SLACK).run_alert(event).to_dict()["failures"]
    assert "AKIAIOSFODNN7EXAMPLE" not in name and len(name) == 100 and name.endswith("...")


def test_a_workflow_run_alert_says_which_workflow_and_task():
    event = RunEvent(event_id="e", type="run.failed", schema_version=1, ts=AT, run_id="r", attempt=1, seq=1,
                     payload={"pipeline": "orders", "stage": "x", "error_type": "E", "workflow": "daily",
                              "task_id": "load_orders", "workflow_run_id": "w1"})
    body = _notifier(SLACK).run_alert(event).to_dict()
    assert (body["workflow"], body["task_id"]) == ("daily", "load_orders") and "workflow_run_id" not in body


def test_a_run_alert_names_the_pipeline_and_table_of_the_event_before_those_of_the_config():
    event = RunEvent(event_id="e", type="run.failed", schema_version=1, ts=AT, run_id="r", attempt=1, seq=1,
                     payload={"pipeline": "task_one", "table": "shop.other", "stage": "x", "error_type": "E"})
    body = _notifier(SLACK).run_alert(event).to_dict()
    assert (body["pipeline"], body["table"], body["owner"]) == ("task_one", "shop.other", "ops team")
    assert body["at"] == "2026-10-28T12:00:00Z"
    bare = RunEvent(event_id="e", type="run.failed", schema_version=1, ts=AT, run_id="r", attempt=1, seq=1, payload={})
    bare_body = _notifier(SLACK).run_alert(bare).to_dict()
    assert (bare_body["pipeline"], bare_body["table"]) == ("orders", "shop.orders")


# ---------------------------------------------------------------------- the senders


def test_a_slack_channel_posts_text_and_nothing_else_to_the_url_in_its_environment(channels_env):
    url, handler = channels_env
    notifier = _notifier(SLACK)
    alert = notifier.monitor_alert(_monitor_result(), "monitor.error", "ok")
    [delivery] = notifier.broadcast(alert)
    assert delivery.ok and delivery.channel == "slack LDP_SLACK_WEBHOOK" and delivery.reason == ""
    [(path, auth, body)] = handler.received
    assert path == "/slack" + SECRET_PATH and auth is None
    assert list(body) == ["text"] and body["text"] == alert.text()
    for needle in ("orders", "shop.orders", "ops team", "freshness", "30.1 hour", "error after 26 hour",
                   "ldp status config/orders.json"):
        assert needle in body["text"]


def test_a_webhook_channel_posts_the_alert_json_with_a_bearer_token(channels_env):
    url, handler = channels_env
    notifier = _notifier(WEBHOOK)
    alert = notifier.monitor_alert(_monitor_result(), "monitor.error", "ok")
    [delivery] = notifier.broadcast(alert)
    assert delivery.ok
    [(path, auth, body)] = handler.received
    assert (path, auth) == ("/hook", "Bearer t0ken-value")
    assert body == alert.to_dict() and body["schema"] == "ldp.alert" and body["version"] == 1


def test_a_webhook_without_a_token_env_sends_no_authorization(channels_env):
    url, handler = channels_env
    notifier = _notifier({"type": "webhook", "url_env": "ALERT_URL"})
    notifier.broadcast(notifier.alert("monitor.error", AT, monitor="freshness"))
    assert handler.received[0][1] is None


def test_a_file_channel_appends_one_json_line_for_each_alert_next_to_the_config(tmp_path):
    notifier = _notifier(FILE, tmp_path=tmp_path)
    first = notifier.monitor_alert(_monitor_result(), "monitor.error", "ok")
    second = notifier.monitor_alert(_monitor_result(status="ok"), "monitor.resolved", "error")
    notifier.broadcast(first)
    notifier.broadcast(second)
    lines = (tmp_path / "alerts" / "out.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in lines] == [first.to_dict(), second.to_dict()]
    assert all(line == json.dumps(json.loads(line), sort_keys=True, ensure_ascii=False) for line in lines)


def test_a_file_channel_without_a_path_writes_to_the_default_file(tmp_path):
    notifier = _notifier({"type": "file"}, tmp_path=tmp_path)
    notifier.broadcast(notifier.alert("run.failed", AT, run_id="r1"))
    assert json.loads((tmp_path / ".ldp" / "alerts.jsonl").read_text())["run_id"] == "r1"


def test_a_channel_gets_only_the_events_that_it_wants(channels_env, tmp_path):
    url, handler = channels_env
    notifier = _notifier({**SLACK, "on": ["monitor.error"]}, {**WEBHOOK, "on": ["run.failed", "monitor.heartbeat"]},
                         {**FILE, "on": ["monitor.resolved"]}, tmp_path=tmp_path)
    notifier.broadcast(notifier.alert("monitor.error", AT, monitor="m"))
    assert [path for path, _, _ in handler.received] == ["/slack" + SECRET_PATH]
    notifier.broadcast(notifier.alert("run.failed", AT, run_id="r"))
    notifier.broadcast(notifier.alert("monitor.heartbeat", AT, monitors=1))
    assert [path for path, _, _ in handler.received][1:] == ["/hook", "/hook"]
    assert not (tmp_path / "alerts" / "out.jsonl").exists()
    notifier.broadcast(notifier.alert("monitor.warn", AT, monitor="m"))
    assert len(handler.received) == 3
    notifier.broadcast(notifier.alert("monitor.resolved", AT, monitor="m"))
    assert (tmp_path / "alerts" / "out.jsonl").exists() and len(handler.received) == 3


def test_a_test_message_goes_to_every_channel_whatever_its_on_says(channels_env, tmp_path):
    url, handler = channels_env
    notifier = _notifier({**SLACK, "on": ["monitor.heartbeat"]}, {**WEBHOOK, "on": ["monitor.warn"]}, FILE,
                         tmp_path=tmp_path)
    deliveries = notifier.test(AT)
    assert [delivery.ok for delivery in deliveries] == [True, True, True]
    assert [delivery.channel for delivery in deliveries] == ["slack LDP_SLACK_WEBHOOK", "webhook ALERT_URL",
                                                             "file alerts/out.jsonl"]
    slack_body, hook_body = handler.received[0][2], handler.received[1][2]
    assert "test message" in slack_body["text"] and "Nothing is wrong" in slack_body["text"]
    assert hook_body["event"] == "alert.test" and hook_body["at"] == "2026-10-28T12:00:00Z"
    assert json.loads((tmp_path / "alerts" / "out.jsonl").read_text())["event"] == "alert.test"


@pytest.mark.parametrize("status, ok", [(200, True), (201, True), (204, True), (299, True), (300, False),
                                        (301, False), (400, False), (404, False), (500, False), (503, False)])
def test_a_status_below_300_is_an_alert_that_arrived_and_any_other_is_not(channels_env, status, ok):
    url, handler = channels_env
    handler.status = status
    [delivery] = _notifier(WEBHOOK).broadcast(Alert("run.failed", AT))
    assert delivery.ok is ok and delivery.reason == ("" if ok else f"it answered HTTP {status}")


def test_the_request_is_a_json_body_with_a_bearer_token_and_a_timeout_and_keeps_the_characters_of_the_text(monkeypatch):
    import requests

    seen = {}

    class Answer:
        status_code = 200

    def post(url, **kwargs):
        seen.update(kwargs, url=url)
        return Answer()

    monkeypatch.setenv("ALERT_URL", "http://webhook.example/hook")
    monkeypatch.setenv("ALERT_TOKEN", "tok")
    monkeypatch.setattr(requests, "post", post)
    [delivery] = _notifier(WEBHOOK, pipeline="caf\u00e9").test(AT)
    assert delivery == Delivery("webhook ALERT_URL", True)
    assert seen["url"] == "http://webhook.example/hook"
    assert seen["timeout"] == TIMEOUT_S == 10.0
    assert seen["headers"] == {"Content-Type": "application/json", "Authorization": "Bearer tok"}
    assert isinstance(seen["data"], bytes) and "caf\u00e9".encode("utf-8") in seen["data"]
    body = json.loads(seen["data"])
    assert body["event"] == "alert.test" and body["pipeline"] == "caf\u00e9" and body["schema"] == "ldp.alert"


def test_slack_gets_the_text_of_the_alert_in_a_text_key_and_no_token_header(monkeypatch):
    import requests

    seen = {}

    class Answer:
        status_code = 204

    def post(url, **kwargs):
        seen.update(kwargs)
        return Answer()

    monkeypatch.setenv("LDP_SLACK_WEBHOOK", "https://hooks.slack.com/services/T/B/XX")
    monkeypatch.setattr(requests, "post", post)
    notifier = _notifier(SLACK)
    [delivery] = notifier.test(AT)
    assert delivery.ok is True
    body = json.loads(seen["data"])
    assert list(body) == ["text"] and body["text"].startswith("ldp alert test on orders (")
    assert seen["headers"] == {"Content-Type": "application/json"}


def test_a_channel_that_answers_with_an_error_status_is_a_delivery_that_says_so_and_a_warning(channels_env, caplog):
    url, handler = channels_env
    handler.status = 500
    notifier = _notifier(SLACK)
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        [delivery] = notifier.broadcast(notifier.alert("monitor.error", AT, monitor="m"))
    assert (delivery.ok, delivery.reason) == (False, "it answered HTTP 500")
    assert "monitor.error alert for orders was not sent to slack LDP_SLACK_WEBHOOK, because it answered HTTP 500" in (
        caplog.text)
    assert "XXSECRETXXTOKEN" not in caplog.text and "127.0.0.1" not in caplog.text


def test_a_channel_that_is_down_is_a_delivery_that_says_so_and_never_an_exception(monkeypatch, caplog):
    monkeypatch.setenv("LDP_SLACK_WEBHOOK", "http://127.0.0.1:9/slack" + SECRET_PATH)
    notifier = _notifier(SLACK)
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        [delivery] = notifier.broadcast(notifier.alert("run.failed", AT))
    assert (delivery.ok, delivery.reason) == (False, "it could not be reached (ConnectionError)")
    assert "XXSECRETXXTOKEN" not in caplog.text and "ConnectionError" in caplog.text


def test_a_channel_that_does_not_answer_in_time_is_a_timeout(monkeypatch):
    import requests

    def slow(*args, **kwargs):
        raise requests.Timeout("read timed out for https://hooks.slack.com/services/T/B/XX")

    monkeypatch.setenv("LDP_SLACK_WEBHOOK", "http://127.0.0.1:9/x")
    monkeypatch.setattr(requests, "post", slow)
    [delivery] = _notifier(SLACK).broadcast(Alert("run.failed", AT))
    assert (delivery.ok, delivery.reason) == (False, "it did not answer in 10 seconds")


def test_a_url_that_is_not_set_or_empty_is_a_warning_and_nothing_is_sent(monkeypatch, caplog):
    monkeypatch.delenv("LDP_SLACK_WEBHOOK", raising=False)
    notifier = _notifier(SLACK)
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        [delivery] = notifier.broadcast(notifier.alert("run.failed", AT))
    assert (delivery.ok, delivery.reason) == (False, "the environment variable LDP_SLACK_WEBHOOK is not set")
    assert "the environment variable LDP_SLACK_WEBHOOK is not set" in caplog.text
    monkeypatch.setenv("LDP_SLACK_WEBHOOK", "")
    [empty] = notifier.broadcast(notifier.alert("run.failed", AT))
    assert (empty.ok, empty.reason) == (False, "the environment variable LDP_SLACK_WEBHOOK is not set")


def test_a_token_that_is_not_set_is_a_warning_and_the_alert_is_sent_without_it(channels_env, monkeypatch, caplog):
    url, handler = channels_env
    monkeypatch.delenv("ALERT_TOKEN")
    notifier = _notifier(WEBHOOK)
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        [delivery] = notifier.broadcast(notifier.alert("run.failed", AT))
    assert delivery.ok and handler.received[0][1] is None
    assert "the environment variable ALERT_TOKEN is not set, so it is sent without a token" in caplog.text


def test_a_token_that_is_empty_is_not_sent_as_a_bearer_and_is_a_warning(channels_env, monkeypatch, caplog):
    url, handler = channels_env
    monkeypatch.setenv("ALERT_TOKEN", "")
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        [delivery] = _notifier(WEBHOOK).broadcast(Alert("run.failed", AT))
    assert delivery.ok and handler.received[0][1] is None
    assert "the environment variable ALERT_TOKEN is not set, so it is sent without a token" in caplog.text


def test_a_url_that_is_no_url_is_a_channel_that_could_not_be_reached_and_the_text_stays_out(monkeypatch):
    monkeypatch.setenv("LDP_SLACK_WEBHOOK", "hooks.example.com/XXSECRETXX")
    delivery = deliver(Channel(kind="slack", url_env="LDP_SLACK_WEBHOOK"), Alert("run.failed", AT))
    assert delivery == Delivery("slack LDP_SLACK_WEBHOOK", False, "it could not be reached (MissingSchema)")


def test_a_file_that_cannot_be_written_is_a_delivery_that_says_so(tmp_path):
    (tmp_path / "alerts").mkdir()
    (tmp_path / "alerts" / "out.jsonl").mkdir()  # a folder where the file should be
    [delivery] = _notifier(FILE, tmp_path=tmp_path).broadcast(Alert("run.failed", AT))
    assert not delivery.ok and re.fullmatch(r"the file could not be written \(\w+\)", delivery.reason), delivery.reason
    assert str(tmp_path) not in delivery.reason


def test_a_path_that_cannot_be_a_path_is_a_delivery_that_says_so(tmp_path):
    delivery = deliver(Channel(kind="file", path="a\0b"), Alert("run.failed", AT), tmp_path)
    assert delivery == Delivery("file a\0b", False, "the file could not be written (ValueError)")


def test_a_file_channel_keeps_the_characters_of_the_text_as_they_are(tmp_path):
    alert = Alert("alert.test", AT, pipeline="caf\u00e9", facts={"zeta": 1, "alpha": 2})
    assert deliver(Channel(kind="file", path="out.jsonl"), alert, tmp_path).ok
    [line] = (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()
    assert "caf\u00e9" in line and "\\u00e9" not in line
    assert json.loads(line)["pipeline"] == "caf\u00e9"


def test_a_sender_that_raises_something_unexpected_is_still_only_a_delivery(channels_env, monkeypatch, caplog):
    import requests

    def broken(*args, **kwargs):
        raise RuntimeError("boom https://hooks.slack.com/services/T/B/XXX")

    monkeypatch.setattr(requests, "post", broken)
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        [delivery] = _notifier(SLACK).broadcast(Alert("run.failed", AT))
    assert (delivery.ok, delivery.reason) == (False, "it failed (RuntimeError)")
    assert "XXX" not in caplog.text and "boom" not in caplog.text


def test_deliver_is_the_function_that_the_notifier_calls_and_it_takes_a_base_dir(tmp_path):
    channel = Channel(kind="file", path="x/y.jsonl")
    delivery = deliver(channel, Alert("run.failed", AT, facts={"run_id": "r"}), tmp_path)
    assert delivery.ok and json.loads((tmp_path / "x" / "y.jsonl").read_text())["run_id"] == "r"


# ---------------------------------------------------------------------- the command to look closer


def test_the_command_names_the_config_file_relative_to_the_working_folder_when_it_is_inside_it(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    path = tmp_path / "config" / "orders.json"
    path.write_text(json.dumps(_spec([SLACK])))
    monkeypatch.chdir(tmp_path)
    config = Config.from_json("config/orders.json")
    assert config.source_path == path.resolve() and command_of(config) == "ldp status config/orders.json"
    monkeypatch.chdir(tmp_path / "config")
    assert command_of(config) == "ldp status orders.json"
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert command_of(config) == f"ldp status {path.resolve()}"


def test_the_command_is_relative_when_the_config_was_given_by_a_path_through_a_symlink(tmp_path, monkeypatch):
    real = tmp_path / "real"
    (real / "config").mkdir(parents=True)
    (real / "config" / "orders.json").write_text(json.dumps(_spec([SLACK])))
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    config = Config.from_json(link / "config" / "orders.json")
    assert config.source_path == link / "config" / "orders.json"
    monkeypatch.chdir(real)
    assert command_of(config) == "ldp status config/orders.json"


def test_a_config_built_by_hand_has_no_command_and_no_source_path(tmp_path):
    config = Config.from_dict(_spec([SLACK]), base_dir=tmp_path)
    assert config.source_path is None and command_of(config) is None
    assert notifier_for(config).command is None
    assert Config.from_dict(_spec([SLACK]), base_dir=tmp_path) == config


def test_a_config_read_from_a_file_equals_the_same_config_built_by_hand(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text(json.dumps(_spec([SLACK])))
    assert Config.from_json(path) == Config.from_dict(_spec([SLACK]), base_dir=tmp_path)
    assert "source_path" not in repr(Config.from_json(path))


def test_the_notifier_of_a_config_knows_its_pipeline_table_owner_and_folder(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text(json.dumps(_spec([SLACK, FILE])))
    notifier = notifier_for(Config.from_json(path))
    assert (notifier.pipeline, notifier.table, notifier.owner, notifier.base_dir) == (
        "orders", "shop.orders", "ops team", tmp_path)
    assert [channel.kind for channel in notifier.channels] == ["slack", "file"]


def test_the_test_message_of_a_notifier_with_no_pipeline_says_a_config(tmp_path):
    notifier = Notifier(channels=(Channel(kind="file", path="out.jsonl"),), base_dir=tmp_path)
    [delivery] = notifier.test(AT)
    assert delivery.ok and json.loads((tmp_path / "out.jsonl").read_text())["message"] == (
        "This is a test message from ldp alert test for a config. Nothing is wrong.")


def test_the_owner_of_a_notifier_is_masked_and_cut(tmp_path):
    spec = _spec([FILE])
    spec["who"] = "ops AKIAIOSFODNN7EXAMPLE " + "w" * 300
    owner = notifier_for(Config.from_dict(spec, base_dir=tmp_path)).owner
    assert "AKIAIOSFODNN7EXAMPLE" not in owner and len(owner) == 200 and owner.endswith("...")


ICEBERG = {"identifier": "shop", "warehouse_path": "warehouse"}


@pytest.mark.parametrize("target, table", [
    ({"name": "orders", "format": "ICEBERG", "catalog": ICEBERG}, "shop.orders"),
    ({"name": "orders", "format": " iceberg ", "catalog": ICEBERG}, "shop.orders"),
    ({"name": "orders", "format": "ICEBERG"}, "orders"),
    ({"name": "orders", "format": "ICEBERG", "catalog": {}}, None),
    ({"format": "ICEBERG", "catalog": ICEBERG}, None),
    ({"name": "orders_csv", "format": "CSV", "path": "out/orders.csv"}, "out/orders.csv"),
    ({"name": "orders_csv", "format": "CSV"}, "orders_csv"),
    ({"format": "CSV"}, None),
])
def test_the_table_of_a_target_is_named_without_opening_anything(target, table):
    config = Config(identifier="orders", metadata={"target": target, "observability": {"alerts": [FILE]}})
    assert notifier_for(config).table == table


def test_a_config_with_no_target_has_a_notifier_with_no_table():
    config = Config(identifier="orders", metadata={"observability": {"alerts": [FILE]}})
    assert notifier_for(config).table is None


def test_the_table_of_a_target_that_is_a_file_is_its_path(tmp_path):
    spec = _spec([FILE])
    spec["metadata"]["target"] = {"name": "orders_csv", "format": "CSV", "path": "out/orders.csv"}
    assert notifier_for(Config.from_dict(spec, base_dir=tmp_path)).table == "out/orders.csv"


def test_a_config_with_a_channel_that_is_not_valid_has_no_notifier(tmp_path):
    with pytest.raises(ConfigError, match=r"^metadata\.observability\.alerts\[0\]\.url_env: is required"):
        notifier_for(Config.from_dict(_spec([{"type": "slack"}]), base_dir=tmp_path))


# ---------------------------------------------------------------------- the sink


def test_sink_from_config_adds_a_notify_sink_to_the_sinks_of_a_config(tmp_path):
    jsonl = _spec([SLACK])
    jsonl["metadata"]["observability"]["sinks"] = ["jsonl"]
    sink = sink_from_config(Config.from_dict(jsonl, base_dir=tmp_path))
    assert isinstance(sink, MultiSink) and [type(item).__name__ for item in sink.sinks] == ["JsonlSink", "NotifySink"]
    alone = sink_from_config(Config.from_dict(_spec([SLACK]), base_dir=tmp_path))
    assert isinstance(alone, NotifySink) and repr(alone) == "NotifySink(['slack LDP_SLACK_WEBHOOK'])"
    assert isinstance(sink_from_config(Config.from_dict(_spec([]), base_dir=tmp_path)), NullSink)
    with pytest.raises(ConfigError, match="alerts"):
        sink_from_config(Config.from_dict(_spec([{"type": "nope"}]), base_dir=tmp_path))


def test_building_the_sink_reads_no_environment_variable_and_sends_nothing(tmp_path, monkeypatch, lineage_server):
    url, handler = lineage_server
    monkeypatch.delenv("LDP_SLACK_WEBHOOK", raising=False)
    sink = sink_from_config(Config.from_dict(_spec([SLACK]), base_dir=tmp_path))
    sink.flush()
    assert handler.received == []


def test_the_sink_holds_the_event_until_the_run_is_flushed_and_sends_one_alert_for_each(channels_env):
    url, handler = channels_env
    sink = NotifySink(_notifier(SLACK, WEBHOOK))
    emitter = RunEmitter(sink, "run-1", context={"pipeline": "orders", "table": "shop.orders"})
    emitter.emit("run.started", {})
    emitter.emit("run.failed", {"stage": "extract", "error_type": "OSError", "message": "disk"})
    emitter.emit("run.finished", {"status": "failed"})
    assert handler.received == []
    emitter.flush()
    assert [path for path, _, _ in handler.received] == ["/slack" + SECRET_PATH, "/hook"]
    emitter.flush()
    assert len(handler.received) == 2, "a second flush sends nothing"


def test_the_sink_does_no_work_for_an_event_that_does_not_end_a_run_badly(monkeypatch):
    notifier = _notifier({**SLACK, "on": list(EVENTS)})
    built = []

    def spy(event):
        built.append(event.type)
        return Alert(event.type, event.ts)

    monkeypatch.setattr(notifier, "run_alert", spy)
    sink = NotifySink(notifier)
    emitter = RunEmitter(sink, "run-1", context={"pipeline": "orders"})
    for kind in ("run.started", "run.extracted", "run.published", "run.finished"):
        emitter.emit(kind, {})
    emitter.flush()
    assert built == [] and sink._pending == []


def test_the_sink_sends_nothing_for_a_run_that_publishes_or_skips(channels_env):
    url, handler = channels_env
    sink = NotifySink(_notifier({**SLACK, "on": list(EVENTS)}))
    emitter = RunEmitter(sink, "run-1", context={"pipeline": "orders"})
    for kind in ("run.started", "run.extracted", "quality.evaluated", "run.published", "run.skipped_duplicate",
                 "run.finished"):
        emitter.emit(kind, {})
    emitter.flush()
    assert handler.received == []


def test_the_sink_carries_the_workflow_tags_of_a_tagged_sink_into_the_alert(channels_env):
    url, handler = channels_env
    notify = NotifySink(_notifier(WEBHOOK))
    tagged = TaggedSink(notify, {"workflow": "daily", "workflow_run_id": "w1", "task_id": "load_orders"})
    emitter = RunEmitter(tagged, "run-1", context={"pipeline": "orders"})
    emitter.emit("run.failed", {"stage": "extract", "error_type": "OSError"})
    emitter.flush()
    body = handler.received[0][2]
    assert (body["workflow"], body["task_id"], body["run_id"], body["attempt"]) == ("daily", "load_orders", "run-1", 1)


def test_each_attempt_of_a_retried_run_that_fails_sends_its_own_alert(channels_env):
    url, handler = channels_env
    sink = NotifySink(_notifier(WEBHOOK))
    for attempt in (1, 2):
        emitter = RunEmitter(sink, "run-1", attempt=attempt, context={"pipeline": "orders"})
        emitter.emit("run.failed", {"stage": "extract", "error_type": "ConnectionError"})
        emitter.flush()
    assert [body["attempt"] for _, _, body in handler.received] == [1, 2]


# ---------------------------------------------------------------------- ldp run


def _lake(tmp_path: Path, alerts, *, rows="order_id,amount\n1,10.5\n2,20.0\n", quality=None, source="orders.csv",
          **extra) -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "orders.csv").write_text(rows)
    spec = _spec(alerts, **extra)
    spec["metadata"]["source"]["path"] = f"../{source}"
    spec["metadata"]["target"]["catalog"]["warehouse_path"] = "../warehouse"
    spec["metadata"]["target"]["write_mode"] = "overwrite"
    if quality is not None:
        spec["metadata"]["quality"] = quality
    path = tmp_path / "config" / "orders.json"
    path.write_text(json.dumps(spec))
    return path


def test_a_run_that_fails_sends_an_alert_and_still_fails_for_the_reason_it_failed(tmp_path, channels_env, capsys):
    url, handler = channels_env
    path = _lake(tmp_path, [SLACK, WEBHOOK, FILE], source="missing.csv")
    assert main(["run", str(path)]) == 1
    assert "FileNotFoundError" in capsys.readouterr().err
    slack = next(body for path_, _, body in handler.received if path_.startswith("/slack"))
    hook = next(body for path_, _, body in handler.received if path_ == "/hook")
    assert slack["text"].splitlines()[0] == "ldp run failed on orders (table shop.orders, owner ops team)"
    assert slack["text"].splitlines()[1] == "stage extract, FileNotFoundError, attempt 1"
    assert hook["event"] == "run.failed" and hook["stage"] == "extract" and hook["error_type"] == "FileNotFoundError"
    assert hook["command"] == f"ldp status {path.resolve()}"
    assert "missing.csv" not in json.dumps(hook) + slack["text"]
    assert json.loads((tmp_path / "config" / "alerts" / "out.jsonl").read_text())["event"] == "run.failed"


def test_a_run_that_a_quality_check_blocks_sends_the_names_of_the_checks_and_no_value(tmp_path, channels_env, capsys):
    url, handler = channels_env
    path = _lake(tmp_path, [WEBHOOK], rows="order_id,status\n1,shipped\n2,secret-status-value\n",
                 quality=[{"check": "accepted_values", "column": "status", "values": ["shipped", "pending"]}])
    assert main(["run", str(path)]) == 3
    [(_, _, body)] = handler.received
    assert body["event"] == "run.blocked_quality" and body["checks_failed"] == 1
    assert body["failures"] == ["accepted_values(status)"]
    assert "secret-status-value" not in json.dumps(body)


def test_a_run_that_publishes_sends_nothing_and_neither_does_one_that_finds_its_key(tmp_path, channels_env, capsys):
    url, handler = channels_env
    path = _lake(tmp_path, [{**SLACK, "on": list(EVENTS)}])
    assert main(["run", str(path), "--idempotency-key", "load-1"]) == 0
    assert main(["run", str(path), "--idempotency-key", "load-1"]) == 0
    assert "skipped" in capsys.readouterr().out
    assert handler.received == []


def test_a_run_is_not_failed_by_a_channel_that_is_down_and_says_so_in_its_log(tmp_path, monkeypatch, caplog, capsys):
    monkeypatch.setenv("LDP_SLACK_WEBHOOK", "http://127.0.0.1:9/slack" + SECRET_PATH)
    broken = _lake(tmp_path / "a", [SLACK], source="missing.csv")
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        assert main(["run", str(broken)]) == 1
    assert "was not sent to slack LDP_SLACK_WEBHOOK" in caplog.text and "XXSECRETXXTOKEN" not in caplog.text
    fine = _lake(tmp_path / "b", [{**SLACK, "on": list(EVENTS)}])
    assert main(["run", str(fine)]) == 0


def test_a_run_with_no_url_in_the_environment_still_finishes_and_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("LDP_SLACK_WEBHOOK", raising=False)
    path = _lake(tmp_path, [SLACK], source="missing.csv")
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        assert main(["run", str(path)]) == 1
    assert "the environment variable LDP_SLACK_WEBHOOK is not set" in caplog.text


def test_the_on_of_a_channel_chooses_which_ends_of_a_run_it_hears(tmp_path, channels_env):
    url, handler = channels_env
    failed_only = _lake(tmp_path / "a", [{**WEBHOOK, "on": ["run.blocked_quality"]}], source="missing.csv")
    assert main(["run", str(failed_only)]) == 1
    assert handler.received == []
    blocked = _lake(tmp_path / "b", [{**WEBHOOK, "on": ["run.failed"]}], rows="order_id,amount\n1,\n",
                    quality=[{"check": "not_null", "columns": ["amount"]}])
    assert main(["run", str(blocked)]) == 3
    assert handler.received == []
    both = _lake(tmp_path / "c", [{**WEBHOOK, "on": ["run.blocked_quality"]}], rows="order_id,amount\n1,\n",
                 quality=[{"check": "not_null", "columns": ["amount"]}])
    assert main(["run", str(both)]) == 3
    assert [body["event"] for _, _, body in handler.received] == ["run.blocked_quality"]


def test_alerts_do_not_change_what_a_run_does_or_its_idempotency(tmp_path, channels_env):
    plain = _lake(tmp_path / "a", [])
    watched = _lake(tmp_path / "b", [SLACK, FILE])
    from local_data_platform.spec import plan

    assert plan(str(plain))["spec_hash"] == plan(str(watched))["spec_hash"]
    assert main(["run", str(plain)]) == main(["run", str(watched)]) == 0


# ---------------------------------------------------------------------- ldp alert test


def test_ldp_alert_test_sends_one_message_to_each_channel_and_says_which_got_it(tmp_path, channels_env, capsys):
    url, handler = channels_env
    path = _lake(tmp_path, [{**SLACK, "on": ["monitor.heartbeat"]}, WEBHOOK, FILE])
    assert main(["alert", "test", str(path)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["slack LDP_SLACK_WEBHOOK: sent", "webhook ALERT_URL: sent", "file alerts/out.jsonl: sent",
                   "3 of 3 channels got the test message"]
    assert [body.get("event") or "text" for _, _, body in handler.received] == ["text", "alert.test"]
    assert (tmp_path / "config" / "alerts" / "out.jsonl").exists()


def test_ldp_alert_test_exits_with_1_when_a_channel_does_not_get_it_and_says_why(
        tmp_path, channels_env, monkeypatch, capsys):
    url, handler = channels_env
    monkeypatch.setenv("ALERT_URL", "http://127.0.0.1:9/hook")
    monkeypatch.delenv("LDP_SLACK_WEBHOOK")
    path = _lake(tmp_path, [SLACK, WEBHOOK, FILE])
    assert main(["alert", "test", str(path)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == ["slack LDP_SLACK_WEBHOOK: not sent, because the environment variable LDP_SLACK_WEBHOOK is not set",
                   "webhook ALERT_URL: not sent, because it could not be reached (ConnectionError)",
                   "file alerts/out.jsonl: sent", "1 of 3 channels got the test message"]


def test_ldp_alert_test_of_a_config_with_no_channel_says_so_and_exits_with_1(tmp_path, capsys):
    path = _lake(tmp_path, [])
    assert main(["alert", "test", str(path)]) == 1
    assert capsys.readouterr().out == ("orders has no channel in metadata.observability.alerts, so there is nothing to "
                                       "test\n")


def test_ldp_alert_without_a_command_prints_help_and_says_that_test_sends_real_messages(capsys):
    assert main(["alert"]) == 1
    assert "test" in capsys.readouterr().out
    assert main(["alert", "test", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "this sends real messages" in text and "Exit status is 0 when every channel got the message" in text


def test_ldp_alert_test_of_a_config_with_an_invalid_channel_stops_with_the_path_of_the_problem(tmp_path, capsys):
    path = _lake(tmp_path, [{"type": "slack"}])
    assert main(["alert", "test", str(path)]) == 1
    assert "metadata.observability.alerts[0].url_env: is required" in capsys.readouterr().err


def test_the_test_message_leaves_the_configs_alerts_alone(tmp_path, channels_env):
    url, handler = channels_env
    path = _lake(tmp_path, [SLACK])
    before = copy.deepcopy(json.loads(path.read_text()))
    main(["alert", "test", str(path)])
    assert json.loads(path.read_text()) == before
