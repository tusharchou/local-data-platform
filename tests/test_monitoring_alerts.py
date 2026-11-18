"""The alerts of ``ldp monitor``: a message for a change of state, and one when a monitor recovers."""

import datetime as dt
import json
import logging
import os
from pathlib import Path

import pytest

from local_data_platform import Config
from local_data_platform.cli import main
from local_data_platform.monitoring.alerts import Before, before, decide, plan_alerts, repeat_due, send_alerts
from local_data_platform.monitoring.history import JSONL_PATH, read_jsonl, read_results
from local_data_platform.monitoring.results import MonitorResult
from local_data_platform.notify import Channel, Notifier

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
SLACK = {"type": "slack", "url_env": "LDP_SLACK_WEBHOOK"}
WEBHOOK = {"type": "webhook", "url_env": "ALERT_URL", "token_env": "ALERT_TOKEN"}
FILE = {"type": "file", "path": "alerts.jsonl"}
EVERYTHING = ["monitor.warn", "monitor.error", "monitor.resolved"]
LATE = {"warn_after": {"count": 2, "period": "day"}, "error_after": {"count": 4, "period": "day"},
        "column": "ordered_at"}
OLD = "order_id,amount,ordered_at\n1,10.5,2026-08-02T08:00:00\n2,20.0,2026-08-02T12:00:00\n"
NEW = "order_id,amount,ordered_at\n3,5.0,2026-08-07T00:30:00\n"
LOOK_OK = "2026-08-03T12:00:00Z"
LOOK_WARN = "2026-08-05T00:00:00Z"
LOOK_ERROR = "2026-08-07T00:00:00Z"
LOOK_FRESH = "2026-08-07T02:00:00Z"


def _result(status: str, minutes: int = 0, monitor: str = "freshness") -> MonitorResult:
    return MonitorResult(monitor=monitor, kind="freshness", status=status,
                         evaluated_at=T0 + dt.timedelta(minutes=minutes), pipeline="orders",
                         table_identifier="shop.orders", observed="1 hour", expected="error after 2", details="d")


def _channel(*on: str, repeat: int | None = None) -> Channel:
    return Channel(kind="file", on=on or ("monitor.error", "monitor.resolved"), repeat_after_minutes=repeat,
                   path="a.jsonl")


# ---------------------------------------------------------------------- the state before a look


def test_a_monitor_that_was_never_looked_at_has_no_state():
    assert before([], "freshness", T0) == Before(None, None, None)
    assert before([_result("ok", monitor="volume.rows")], "freshness", T0) == Before(None, None, None)


def test_the_state_is_the_last_status_that_was_not_unknown_and_when_that_stretch_began():
    history = [_result("ok", 0), _result("error", 10), _result("error", 20), _result("unknown", 30),
               _result("error", 40)]
    now = T0 + dt.timedelta(minutes=50)
    assert before(history, "freshness", now) == Before("error", T0 + dt.timedelta(minutes=10),
                                                       T0 + dt.timedelta(minutes=40))


def test_unknown_results_alone_leave_the_status_open_and_keep_the_time_of_the_last_look():
    assert before([_result("unknown", 0), _result("unknown", 5)], "freshness", T0 + dt.timedelta(minutes=9)) == Before(
        None, None, T0 + dt.timedelta(minutes=5))


def test_a_stretch_ends_where_the_status_changed_and_a_longer_history_does_not_matter():
    history = [_result("error", 0), _result("ok", 10), _result("warn", 20), _result("warn", 30), _result("ok", 40),
               _result("ok", 50)]
    assert before(history, "freshness", T0 + dt.timedelta(minutes=60)) == Before(
        "ok", T0 + dt.timedelta(minutes=40), T0 + dt.timedelta(minutes=50))
    assert before(history[:4], "freshness", T0 + dt.timedelta(minutes=60)).entered == T0 + dt.timedelta(minutes=20)


def test_results_made_after_now_are_not_history_and_one_made_at_now_is():
    history = [_result("ok", 0), _result("error", 30)]
    assert before(history, "freshness", T0 + dt.timedelta(minutes=10)).status == "ok"
    assert before(history, "freshness", T0 + dt.timedelta(minutes=30)).status == "error"
    assert before(history, "freshness", T0 - dt.timedelta(minutes=1)) == Before(None, None, None)


def test_the_history_may_be_in_any_order():
    shuffled = [_result("error", 30), _result("ok", 0), _result("error", 20), _result("error", 10)]
    assert before(shuffled, "freshness", T0 + dt.timedelta(minutes=40)) == Before(
        "error", T0 + dt.timedelta(minutes=10), T0 + dt.timedelta(minutes=30))


# ---------------------------------------------------------------------- when a repeat is due


@pytest.mark.parametrize("last, now, due", [
    (0, 10, False), (0, 59, False), (0, 60, True), (0, 61, True), (59, 60, True), (60, 61, False),
    (61, 119, False), (61, 120, True), (0, 600, True), (30, 30, False), (119, 121, True)])
def test_a_repeat_is_due_when_a_multiple_of_the_minutes_falls_between_the_last_look_and_this_one(last, now, due):
    entered = T0
    assert repeat_due(entered, T0 + dt.timedelta(minutes=last), T0 + dt.timedelta(minutes=now), 60) is due


def test_a_repeat_is_counted_from_the_first_look_that_saw_the_state():
    entered = T0 + dt.timedelta(minutes=17)
    assert not repeat_due(entered, T0 + dt.timedelta(minutes=70), T0 + dt.timedelta(minutes=76), 60)
    assert repeat_due(entered, T0 + dt.timedelta(minutes=70), T0 + dt.timedelta(minutes=78), 60)


# ---------------------------------------------------------------------- what a channel sends


@pytest.mark.parametrize("on, was, now, expected", [
    # the default channel hears errors and recoveries
    ((), None, "error", ("monitor.error", False)), ((), "ok", "error", ("monitor.error", False)),
    ((), "error", "error", None), ((), "error", "ok", ("monitor.resolved", False)), ((), "ok", "ok", None),
    ((), None, "ok", None), ((), "ok", "warn", None), ((), "warn", "warn", None), ((), None, "warn", None),
    ((), "warn", "error", ("monitor.error", False)), ((), "error", "warn", ("monitor.resolved", False)),
    ((), "warn", "ok", None),
    # a channel that hears warnings too
    (EVERYTHING, "ok", "warn", ("monitor.warn", False)), (EVERYTHING, None, "warn", ("monitor.warn", False)),
    (EVERYTHING, "warn", "error", ("monitor.error", False)), (EVERYTHING, "error", "warn", ("monitor.warn", False)),
    (EVERYTHING, "warn", "ok", ("monitor.resolved", False)), (EVERYTHING, "error", "ok", ("monitor.resolved", False)),
    (EVERYTHING, "warn", "warn", None), (EVERYTHING, "error", "error", None),
    # a channel that hears only warnings
    (("monitor.warn", "monitor.resolved"), "ok", "error", None),
    (("monitor.warn", "monitor.resolved"), "ok", "warn", ("monitor.warn", False)),
    (("monitor.warn", "monitor.resolved"), "warn", "error", ("monitor.resolved", False)),
    # a channel that never hears a recovery, and one that hears only a recovery
    (("monitor.error",), "error", "ok", None), (("monitor.resolved",), "error", "ok", None),
    (("monitor.resolved",), None, "error", None),
    # unknown never raises an alert and never clears one
    ((), "error", "unknown", None), ((), "ok", "unknown", None), ((), None, "unknown", None),
    (EVERYTHING, "warn", "unknown", None),
])
def test_a_channel_sends_for_a_change_of_state_that_it_hears_and_not_for_a_state(on, was, now, expected):
    earlier = Before(was, T0 if was else None, T0 if was else None)
    assert decide(_channel(*on), earlier, now, T0 + dt.timedelta(minutes=5)) == expected


@pytest.mark.parametrize("on, status, event", [
    (("monitor.error", "monitor.resolved"), "error", "monitor.error"),
    (("monitor.warn", "monitor.resolved"), "warn", "monitor.warn"),
    (tuple(EVERYTHING), "warn", "monitor.warn"), (tuple(EVERYTHING), "error", "monitor.error")])
def test_a_channel_with_a_repeat_says_a_state_again_and_marks_it(on, status, event):
    channel = _channel(*on, repeat=60)
    earlier = Before(status, T0, T0 + dt.timedelta(minutes=30))
    assert decide(channel, earlier, status, T0 + dt.timedelta(minutes=30)) is None
    assert decide(channel, earlier, status, T0 + dt.timedelta(minutes=61)) == (event, True)
    assert decide(_channel(*on), earlier, status, T0 + dt.timedelta(minutes=61)) is None, "no repeat, no alert"


def test_a_repeat_is_for_a_state_that_the_channel_hears_and_needs_a_first_look():
    channel = _channel("monitor.error", repeat=60)
    stale = Before("warn", T0, T0 + dt.timedelta(minutes=10))
    assert decide(channel, stale, "warn", T0 + dt.timedelta(minutes=300)) is None
    assert decide(channel, Before("ok", T0, T0), "ok", T0 + dt.timedelta(minutes=300)) is None
    assert decide(channel, Before("error", None, None), "error", T0 + dt.timedelta(minutes=300)) is None


def test_a_change_of_state_is_said_once_and_not_again_as_a_repeat_at_the_same_look():
    channel = _channel("monitor.error", repeat=60)
    earlier = Before("ok", T0, T0 + dt.timedelta(minutes=61))
    assert decide(channel, earlier, "error", T0 + dt.timedelta(minutes=122)) == ("monitor.error", False)


# ---------------------------------------------------------------------- planning the alerts of a look


def _notifier(*channels: Channel) -> Notifier:
    return Notifier(channels=channels, pipeline="orders", table="shop.orders", owner="ops")


def test_the_alerts_of_a_look_have_their_channel_and_say_what_changed():
    slack, hook = Channel(kind="slack", url_env="S"), Channel(kind="webhook", url_env="H", on=tuple(EVERYTHING))
    history = [_result("ok", 0), _result("ok", 10)]
    results = [_result("warn", 20), _result("error", 20, monitor="volume.rows"), _result("ok", 20, monitor="schema")]
    planned = plan_alerts(_notifier(slack, hook), results, history, T0 + dt.timedelta(minutes=20))
    assert [(channel.kind, alert.event, alert.facts["monitor"]) for channel, alert in planned] == [
        ("webhook", "monitor.warn", "freshness"), ("slack", "monitor.error", "volume.rows"),
        ("webhook", "monitor.error", "volume.rows")]
    warn = planned[0][1]
    assert warn.facts["status"] == "warn" and warn.facts["previous_status"] == "ok" and "repeat" not in warn.facts
    assert (warn.pipeline, warn.table, warn.owner) == ("orders", "shop.orders", "ops")


def test_a_monitor_that_is_looked_at_for_the_first_time_has_no_previous_status_in_its_alert():
    [(_, alert)] = plan_alerts(_notifier(_channel()), [_result("error")], [], T0)
    assert alert.event == "monitor.error" and "previous_status" not in alert.to_dict()


def test_a_monitor_that_recovers_says_what_it_was():
    history = [_result("error", 0), _result("error", 10)]
    [(_, alert)] = plan_alerts(_notifier(_channel()), [_result("ok", 20)], history, T0 + dt.timedelta(minutes=20))
    assert (alert.event, alert.facts["status"], alert.facts["previous_status"]) == ("monitor.resolved", "ok", "error")


def test_a_look_that_changes_nothing_plans_nothing():
    history = [_result("ok", 0)]
    assert plan_alerts(_notifier(_channel(*EVERYTHING)), [_result("ok", 10)], history, T0) == []
    assert plan_alerts(_notifier(_channel()), [], history, T0) == []


def test_two_monitors_are_two_states_and_one_recovering_does_not_clear_the_other():
    history = [_result("error", 0, "freshness"), _result("error", 0, "volume.rows")]
    results = [_result("ok", 10, "freshness"), _result("error", 10, "volume.rows")]
    planned = plan_alerts(_notifier(_channel()), results, history, T0 + dt.timedelta(minutes=10))
    assert [(alert.event, alert.facts["monitor"]) for _, alert in planned] == [("monitor.resolved", "freshness")]


def test_an_unknown_look_between_two_errors_says_nothing_and_one_before_a_recovery_still_resolves():
    history = [_result("error", 0), _result("unknown", 10)]
    assert plan_alerts(_notifier(_channel()), [_result("error", 20)], history, T0 + dt.timedelta(minutes=20)) == []
    [(_, alert)] = plan_alerts(_notifier(_channel()), [_result("ok", 20)], history, T0 + dt.timedelta(minutes=20))
    assert alert.event == "monitor.resolved"


def test_a_repeat_is_planned_with_the_repeat_mark():
    history = [_result("error", 0), _result("error", 30)]
    [(_, alert)] = plan_alerts(_notifier(_channel(repeat=60)), [_result("error", 61)], history,
                               T0 + dt.timedelta(minutes=61))
    assert alert.event == "monitor.error" and alert.facts["repeat"] is True
    assert alert.facts["previous_status"] == "error"


def test_sending_the_planned_alerts_and_the_heartbeat_reports_each_delivery(tmp_path):
    hearing = Channel(kind="file", path="a.jsonl", on=("monitor.error", "monitor.heartbeat"))
    deaf = Channel(kind="file", path="b.jsonl", on=("monitor.resolved",))
    notifier = Notifier(channels=(hearing, deaf), pipeline="orders", base_dir=tmp_path)
    results = [_result("error")]
    planned = plan_alerts(notifier, results, [], T0)
    sent = send_alerts(notifier, planned, results, T0)
    assert [(delivery.channel, delivery.ok) for delivery in sent.alerts] == [("file a.jsonl", True)]
    assert [(delivery.channel, delivery.ok) for delivery in sent.heartbeats] == [("file a.jsonl", True)]
    events = [json.loads(line)["event"] for line in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert events == ["monitor.error", "monitor.heartbeat"] and not (tmp_path / "b.jsonl").exists()


# ---------------------------------------------------------------------- ldp monitor


def _config(tmp_path: Path, alerts, identifier: str = "orders", sla="default", rows: str | None = OLD,
            who: str = "ops team") -> Path:
    """A config of a table that is loaded from a CSV, with a freshness SLA on the newest ``ordered_at``."""
    folder = tmp_path / "config"
    folder.mkdir(parents=True, exist_ok=True)
    if rows is not None:
        (tmp_path / f"{identifier}.csv").write_text(rows)
    metadata = {"source": {"name": identifier, "format": "CSV", "path": f"../{identifier}.csv"},
                "target": {"name": identifier, "format": "ICEBERG", "write_mode": "overwrite",
                           "catalog": {"identifier": "shop", "warehouse_path": "../warehouse"}},
                "observability": {"alerts": alerts}}
    if sla is not None:
        metadata["sla"] = {"freshness": LATE} if sla == "default" else sla
    path = folder / f"{identifier}.json"
    path.write_text(json.dumps({"identifier": identifier, "who": who, "metadata": metadata}))
    return path


def _monitor(path: Path, when: str, capsys, *extra: str) -> tuple[int, str]:
    capsys.readouterr()  # what a run or an earlier look printed is not what this look prints
    code = main(["monitor", str(path), "--now", when, *extra])
    return code, capsys.readouterr().out


def _load(path: Path, rows: str | None = None) -> None:
    if rows is not None:
        (path.parent.parent / f"{path.stem}.csv").write_text(rows)
    assert main(["run", str(path)]) == 0


@pytest.fixture
def hooks(lineage_server, monkeypatch):
    """The Slack and webhook channels of a config point at the local server. Yields the handler of the server."""
    url, handler = lineage_server
    handler.status = 200
    monkeypatch.setenv("LDP_SLACK_WEBHOOK", url + "/slack")
    monkeypatch.setenv("ALERT_URL", url + "/hook")
    monkeypatch.setenv("ALERT_TOKEN", "t0ken")
    return handler


def _events(handler, path: str | None = None) -> list[str]:
    return [body["event"] if "event" in body else "text" for where, _, body in handler.received
            if path is None or where == path]


def test_a_table_that_goes_late_sends_one_alert_to_each_channel_and_then_stays_quiet(tmp_path, hooks, capsys):
    path = _config(tmp_path, [SLACK, WEBHOOK, FILE])
    _load(path)
    assert _monitor(path, LOOK_OK, capsys)[0] == 0
    assert hooks.received == [] and not (tmp_path / "config" / "alerts.jsonl").exists()
    code, out = _monitor(path, LOOK_ERROR, capsys)
    assert code == 2
    assert out.splitlines()[-1] == ("Alerts sent to slack LDP_SLACK_WEBHOOK (1), webhook ALERT_URL (1), "
                                    "file alerts.jsonl (1)")
    assert _events(hooks) == ["text", "monitor.error"]
    body = hooks.received[1][2]
    assert (body["monitor"], body["status"], body["previous_status"], body["pipeline"], body["owner"]) == (
        "freshness", "error", "ok", "orders", "ops team")
    assert body["observed"].startswith("4.")
    assert body["expected"] == "warn after 2 day and error after 4 day on column ordered_at"
    assert "newest ordered_at" in body["details"] and body["command"].endswith("config/orders.json")
    assert json.loads((tmp_path / "config" / "alerts.jsonl").read_text())["event"] == "monitor.error"
    for later in ("2026-08-07T01:00:00Z", "2026-08-08T00:00:00Z", "2026-08-09T00:00:00Z"):
        code, out = _monitor(path, later, capsys)
        assert code == 2 and "Alerts sent" not in out
    assert len(hooks.received) == 2, "the same state says nothing again"


def test_a_table_that_is_loaded_again_sends_one_resolved_alert(tmp_path, hooks, capsys):
    path = _config(tmp_path, [WEBHOOK, FILE])
    _load(path)
    _monitor(path, LOOK_ERROR, capsys)
    _load(path, NEW)
    code, out = _monitor(path, LOOK_FRESH, capsys)
    assert code == 0 and out.splitlines()[-1] == "Alerts sent to webhook ALERT_URL (1), file alerts.jsonl (1)"
    assert _events(hooks) == ["monitor.error", "monitor.resolved"]
    resolved = hooks.received[1][2]
    assert (resolved["status"], resolved["previous_status"]) == ("ok", "error")
    _monitor(path, "2026-08-07T03:00:00Z", capsys)
    assert _events(hooks) == ["monitor.error", "monitor.resolved"], "an ok that stays ok says nothing"
    lines = (tmp_path / "config" / "alerts.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["monitor.error", "monitor.resolved"]


def test_a_table_that_is_broken_at_the_first_look_alerts_at_once(tmp_path, hooks, capsys):
    path = _config(tmp_path, [WEBHOOK])
    _load(path)
    assert _monitor(path, LOOK_ERROR, capsys)[0] == 2
    assert _events(hooks) == ["monitor.error"]
    assert "previous_status" not in hooks.received[0][2]


def test_a_channel_that_hears_warnings_gets_the_warning_and_then_the_error_and_the_other_only_the_error(
        tmp_path, hooks, capsys):
    path = _config(tmp_path, [{**WEBHOOK, "on": EVERYTHING}, SLACK])
    _load(path)
    _monitor(path, LOOK_OK, capsys)
    _monitor(path, LOOK_WARN, capsys)
    assert [(where, body["event"]) for where, _, body in hooks.received] == [("/hook", "monitor.warn")]
    _monitor(path, LOOK_ERROR, capsys)
    assert [(where, body.get("event", "text")) for where, _, body in hooks.received] == [
        ("/hook", "monitor.warn"), ("/hook", "monitor.error"), ("/slack", "text")]
    assert hooks.received[1][2]["previous_status"] == "warn"
    _load(path, NEW)
    _monitor(path, LOOK_FRESH, capsys)
    assert _events(hooks, "/hook") == ["monitor.warn", "monitor.error", "monitor.resolved"]
    assert _events(hooks, "/slack") == ["text", "text"]


def test_a_channel_with_a_repeat_says_the_error_again_when_the_minutes_have_passed(tmp_path, hooks, capsys):
    path = _config(tmp_path, [{**WEBHOOK, "repeat_after_minutes": 60}])
    _load(path)
    _monitor(path, LOOK_ERROR, capsys)
    for when in ("2026-08-07T00:30:00Z", "2026-08-07T00:59:00Z"):
        assert "Alerts sent" not in _monitor(path, when, capsys)[1]
    again = _monitor(path, "2026-08-07T01:01:00Z", capsys)
    assert again[1].splitlines()[-1] == "Alerts sent to webhook ALERT_URL (1)"
    assert "Alerts sent" not in _monitor(path, "2026-08-07T01:30:00Z", capsys)[1]
    _monitor(path, "2026-08-07T02:05:00Z", capsys)
    assert [(body["event"], body.get("repeat")) for _, _, body in hooks.received] == [
        ("monitor.error", None), ("monitor.error", True), ("monitor.error", True)]


def test_a_look_after_a_long_gap_repeats_once_and_not_for_each_period_that_it_missed(tmp_path, hooks, capsys):
    path = _config(tmp_path, [{**WEBHOOK, "repeat_after_minutes": 60}])
    _load(path)
    _monitor(path, LOOK_ERROR, capsys)
    _monitor(path, "2026-08-10T00:00:00Z", capsys)
    assert _events(hooks) == ["monitor.error", "monitor.error"]


def test_the_alerts_of_a_look_are_decided_before_it_is_recorded_so_the_same_look_twice_says_it_once(
        tmp_path, hooks, capsys):
    path = _config(tmp_path, [WEBHOOK])
    _load(path)
    _monitor(path, LOOK_ERROR, capsys)
    _monitor(path, LOOK_ERROR, capsys)
    assert _events(hooks) == ["monitor.error"]


def test_a_look_at_a_moment_before_the_history_does_not_see_what_came_after_it(tmp_path, hooks, capsys):
    path = _config(tmp_path, [WEBHOOK])
    _load(path)
    _monitor(path, LOOK_ERROR, capsys)
    assert _events(hooks) == ["monitor.error"]
    _monitor(path, LOOK_OK, capsys)  # earlier than the error that is in the history already
    assert _events(hooks) == ["monitor.error"]


def test_the_heartbeat_is_a_ping_after_each_completed_look_with_how_many_monitors_are_in_each_status(
        tmp_path, hooks, capsys):
    path = _config(tmp_path, [{**WEBHOOK, "on": ["monitor.heartbeat"]}, {**FILE, "on": ["monitor.error"]}])
    _load(path)
    _monitor(path, LOOK_OK, capsys)
    _monitor(path, LOOK_ERROR, capsys)
    _monitor(path, "2026-08-08T00:00:00Z", capsys)
    assert _events(hooks) == ["monitor.heartbeat"] * 3
    first, second, third = (body for _, _, body in hooks.received)
    assert (first["results"], first["monitors"], second["results"], third["results"]) == (
        {"ok": 1}, 1, {"error": 1}, {"error": 1})
    assert [body["at"] for _, _, body in hooks.received] == [LOOK_OK, LOOK_ERROR, "2026-08-08T00:00:00Z"]
    lines = (tmp_path / "config" / "alerts.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["monitor.error"]


def test_the_heartbeat_is_not_in_the_alerts_sent_line_and_a_monitor_that_is_unknown_still_pings(
        tmp_path, hooks, capsys):
    path = _config(tmp_path, [{**WEBHOOK, "on": ["monitor.heartbeat"]}],
                   sla={"volume": {"min_rows": 1, "max_change_pct": 50}})
    _load(path)
    code, out = _monitor(path, LOOK_OK, capsys)
    assert code == 0 and "Alerts sent" not in out
    assert hooks.received[0][2]["results"] in ({"ok": 1, "unknown": 1}, {"unknown": 2}, {"ok": 2})
    assert hooks.received[0][2]["monitors"] == 2


def test_a_config_that_could_not_be_looked_at_sends_no_heartbeat_so_that_a_dead_mans_switch_notices(
        tmp_path, hooks, capsys, monkeypatch):
    good = _config(tmp_path, [{**WEBHOOK, "on": ["monitor.heartbeat"]}], identifier="good")
    broken = _config(tmp_path, [{**WEBHOOK, "on": ["monitor.heartbeat"]}], identifier="broken")
    _load(good)
    _load(broken)
    from local_data_platform.monitoring import evaluation

    real = evaluation.evaluate

    def evaluate(config, now=None):
        if config.identifier == "broken":
            raise RuntimeError("the catalog is gone")
        return real(config, now)

    monkeypatch.setattr(evaluation, "evaluate", evaluate)
    code = main(["monitor", str(tmp_path / "config"), "--now", LOOK_OK])
    assert code == 1
    assert [(body["pipeline"], body["event"]) for _, _, body in hooks.received] == [("good", "monitor.heartbeat")]
    assert "broken.json: RuntimeError: the catalog is gone" in capsys.readouterr().err


def test_a_config_with_no_sla_has_nothing_to_look_at_and_sends_no_heartbeat(tmp_path, hooks, capsys):
    path = _config(tmp_path, [{**WEBHOOK, "on": ["monitor.heartbeat"]}], sla=None)
    _load(path)
    code, out = _monitor(path, LOOK_OK, capsys)
    assert code == 0 and "has no metadata.sla" in out and hooks.received == []


def test_each_config_of_a_folder_alerts_its_own_channels(tmp_path, hooks, capsys, monkeypatch):
    server = os.environ["LDP_SLACK_WEBHOOK"].removesuffix("/slack")
    monkeypatch.setenv("ALERT_URL", server + "/second")
    first = _config(tmp_path, [SLACK], identifier="first", who="team one")
    second = _config(tmp_path, [WEBHOOK], identifier="second", who="team two")
    _load(first)
    _load(second)
    code, out = _monitor(tmp_path / "config", LOOK_ERROR, capsys)
    assert code == 2
    others = [(where, body.get("pipeline"), body.get("owner")) for where, _, body in hooks.received
              if where != "/slack"]
    assert others == [("/second", "second", "team two")]
    slack = [body["text"] for where, _, body in hooks.received if where == "/slack"]
    assert len(slack) == 1 and "first" in slack[0] and "team one" in slack[0]
    assert out.splitlines()[-1] == "Alerts sent to slack LDP_SLACK_WEBHOOK (1), webhook ALERT_URL (1)"


def test_the_line_of_alerts_sent_counts_the_alerts_of_a_channel_across_the_configs_of_a_folder(
        tmp_path, hooks, capsys):
    for name in ("first", "second"):
        _load(_config(tmp_path, [SLACK], identifier=name))
    code, out = _monitor(tmp_path / "config", LOOK_ERROR, capsys)
    assert code == 2 and len(hooks.received) == 2
    assert out.splitlines()[-1] == "Alerts sent to slack LDP_SLACK_WEBHOOK (2)"


def test_json_output_stays_json_when_alerts_are_sent(tmp_path, hooks, capsys):
    path = _config(tmp_path, [WEBHOOK])
    _load(path)
    code, out = _monitor(path, LOOK_ERROR, capsys, "--json")
    assert code == 2 and [item["status"] for item in json.loads(out)] == ["error"]
    assert _events(hooks) == ["monitor.error"]


def test_a_channel_that_is_down_does_not_change_the_exit_status_or_stop_the_results_being_recorded(
        tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv("ALERT_URL", "http://127.0.0.1:9/hook")
    path = _config(tmp_path, [WEBHOOK, FILE])
    _load(path)
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        code, out = _monitor(path, LOOK_ERROR, capsys)
    assert code == 2
    assert "was not sent to webhook ALERT_URL, because it could not be reached" in caplog.text
    assert out.splitlines()[-1] == "Alerts sent to file alerts.jsonl (1)"
    assert [result.status for result in read_jsonl(tmp_path / "config" / JSONL_PATH)] == ["error"]


def test_an_alert_that_failed_to_arrive_is_not_sent_again_at_the_next_look_unless_there_is_a_repeat(
        tmp_path, hooks, monkeypatch, capsys):
    path = _config(tmp_path, [{**WEBHOOK, "repeat_after_minutes": 60}])
    _load(path)
    hooks.status = 500
    _monitor(path, LOOK_ERROR, capsys)
    hooks.status = 200
    _monitor(path, "2026-08-07T00:30:00Z", capsys)
    assert len(hooks.received) == 1, "the one alert of the change was lost, and nothing is sent for the same state"
    _monitor(path, "2026-08-07T01:05:00Z", capsys)
    assert [body.get("repeat") for _, _, body in hooks.received] == [None, True]


def test_a_config_with_a_channel_that_is_not_valid_is_a_failure_with_the_path_and_is_not_looked_at(
        tmp_path, hooks, capsys):
    path = _config(tmp_path, [SLACK])
    _load(path)
    _config(tmp_path, [{"type": "slack"}], rows=None)  # a run would refuse this config too, so it is changed afterwards
    capsys.readouterr()
    code = main(["monitor", str(path), "--now", LOOK_ERROR])
    captured = capsys.readouterr()
    assert code == 1
    assert "orders.json: metadata.observability.alerts[0].url_env: is required" in captured.err
    assert "freshness" not in captured.out and not (tmp_path / "config" / JSONL_PATH).exists()


def test_the_history_holds_the_results_and_nothing_about_alerts(tmp_path, hooks, capsys):
    plain = _config(tmp_path / "a", [])
    watched = _config(tmp_path / "b", [WEBHOOK, FILE])
    _load(plain)
    _load(watched)
    _monitor(plain, LOOK_ERROR, capsys)
    _monitor(watched, LOOK_ERROR, capsys)
    [a] = read_jsonl(plain.parent / JSONL_PATH)
    [b] = read_jsonl(watched.parent / JSONL_PATH)
    assert a.metrics.keys() == b.metrics.keys()
    assert (a.status, a.observed, a.details.split(",")[0]) == (b.status, b.observed, b.details.split(",")[0])
    assert "alert" not in json.dumps(b.to_dict())


def test_a_config_with_alerts_reads_and_writes_the_same_history_as_one_without(tmp_path, hooks, capsys):
    path = _config(tmp_path, [WEBHOOK])
    _load(path)
    _monitor(path, LOOK_ERROR, capsys)
    config = Config.from_json(path)
    assert [result.status for result in read_results(config)] == ["error"]


def test_the_monitor_help_and_the_plan_of_a_config_with_alerts_still_work(tmp_path, capsys):
    path = _config(tmp_path, [SLACK, WEBHOOK, FILE])
    assert main(["monitor", "--help"]) == 0
    assert "alert channels" in " ".join(capsys.readouterr().out.split())
    assert main(["plan", str(path)]) == 0
