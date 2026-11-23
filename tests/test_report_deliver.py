"""Sending a report: the keys of the deliveries, the Slack message, and ``ldp report send`` on a gold table.

The Slack message is tested on runs made by hand, so every number in it is one that the test wrote down, and it is
compared with a golden file in ``tests/golden/report/``. After a change that is meant to alter it, rewrite the file with
``LDP_UPDATE_GOLDEN=1 pytest tests/test_report_deliver.py`` and read the difference in git before committing.

Then a gold table with a config and a history of runs is sent to a Slack destination, a webhook and a file. Every post
goes to the local server of ``tests/conftest.py`` and none leaves the machine, and no test waits for a retry, because
the waits are replaced by a list of what they would have been.
"""

import datetime as dt
import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform import notify
from local_data_platform.cli import main
from local_data_platform.events import MemorySink
from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.report import deliver as report_deliver
from local_data_platform.report.build import ReportBuildError, build_report
from local_data_platform.report.deliver import (
    DELIVERED,
    FAILED,
    MAX_COLUMNS,
    MAX_KPIS,
    SKIPPED,
    WOULD_SEND,
    channel_of,
    delivered_keys,
    delivery_key,
    ordered,
    send_report,
    slack_payload,
)
from local_data_platform.report.spec import Destination, Freshness, load_report
from tests.report_support import BUILT_AT, UTC, golden_run, make_run

GOLDEN = Path(__file__).parent / "golden" / "report"
UPDATE_GOLDEN = os.environ.get("LDP_UPDATE_GOLDEN") == "1"
SECRET = "XXSECRETXXTOKEN"
HOSTILE = "<!channel> <@U0123> <https://evil.example|click> & `tick` ```"
WINDOW = "2026-07-31/2026-08-02"
SPAN = "2026-07-31T00:00:00Z/2026-08-02T00:00:00Z"
START, END = dt.datetime(2026, 7, 31, tzinfo=UTC), dt.datetime(2026, 8, 2, tzinfo=UTC)


# --------------------------------------------------------------------------- the keys, the channels and the order


def test_the_key_of_a_delivery_is_the_hash_of_the_report_the_window_and_the_destination():
    text = '["ldp/v1","report.delivery","daily","2026-07-31T00:00:00Z","2026-08-02T00:00:00Z","team"]'

    key = delivery_key("daily", (START, END), "team")

    assert key == hashlib.sha256(text.encode("utf-8")).hexdigest() and re.fullmatch(r"[0-9a-f]{64}", key)


def test_the_key_changes_with_each_of_the_four_things_that_it_is_made_of():
    base = delivery_key("daily", (START, END), "team")
    day = dt.timedelta(days=1)

    others = {delivery_key("weekly", (START, END), "team"), delivery_key("daily", (START + day, END), "team"),
              delivery_key("daily", (START, END + day), "team"), delivery_key("daily", (START, END), "hook")}

    assert len(others | {base}) == 5


def test_the_key_is_the_same_for_the_same_instants_whatever_zone_they_are_written_in():
    india = dt.timezone(dt.timedelta(hours=5, minutes=30))

    assert delivery_key("daily", (START.astimezone(india), END.astimezone(india)), "team") == delivery_key(
        "daily", (START, END), "team")


def test_the_names_that_could_run_together_do_not_make_the_same_key():
    assert delivery_key("a", (START, END), "bc") != delivery_key("ab", (START, END), "c")
    assert delivery_key('a","b', (START, END), "c") != delivery_key("a", (START, END), 'b","c')


def test_a_slack_destination_is_a_channel_that_wants_https_and_names_the_variable_of_its_address():
    item = Destination("team", "slack", {"webhook_env": "SLACK_URL"})

    assert channel_of(item) == notify.Channel(kind="slack", name="team", url_env="SLACK_URL", retries=3,
                                              https_only=True)
    assert channel_of(Destination("team", "slack", {"webhook_env": "S", "retries": 0})).retries == 0


def test_a_webhook_destination_is_a_channel_that_wants_https_with_headers_and_retries_of_its_own():
    item = Destination("hook", "webhook", {"url_env": "HOOK_URL", "headers_env": "HOOK_HEADERS", "retries": 7})

    assert channel_of(item) == notify.Channel(kind="webhook", name="hook", url_env="HOOK_URL",
                                              headers_env="HOOK_HEADERS", retries=7, https_only=True)
    assert channel_of(Destination("hook", "webhook", {"url_env": "H"})) == notify.Channel(
        kind="webhook", name="hook", url_env="H", retries=notify.RETRIES, https_only=True)


def test_a_file_destination_is_a_channel_with_a_path_and_no_rule_about_https():
    channel = channel_of(Destination("page", "file", {"path": "out/a.html", "public_url": "https://x.example/a"}))

    assert channel == notify.Channel(kind="file", name="page", path="out/a.html") and channel.https_only is False
    assert channel.label == "file page"


def test_the_files_are_delivered_before_the_others_and_each_group_keeps_the_order_of_the_report():
    items = [Destination("a", "slack", {}), Destination("b", "file", {}), Destination("c", "webhook", {}),
             Destination("d", "file", {}), Destination("e", "slack", {})]

    assert [item.name for item in ordered(items)] == ["b", "d", "a", "c", "e"] and ordered([]) == []


# --------------------------------------------------------------------------- the Slack message


def texts(message: dict) -> list[str]:
    """Every text of a message, the top one first, the headers, the sections, the fields and the context elements."""
    found = [message["text"]]
    for block in message["blocks"]:
        if isinstance(block.get("text"), dict):
            found.append(block["text"]["text"])
        found += [field["text"] for field in block.get("fields", [])]
        found += [element["text"] for element in block.get("elements", [])]
    return found


LINK = "https://example.com/reports/daily.html"


def test_the_message_of_the_golden_report_is_what_its_golden_file_says():
    text = json.dumps(slack_payload(golden_run(), link=LINK), indent=2, ensure_ascii=False) + "\n"
    path = GOLDEN / "slack.json"
    if UPDATE_GOLDEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    assert path.is_file(), f"{path} is missing. Write it with LDP_UPDATE_GOLDEN=1 pytest tests/test_report_deliver.py"
    expected = path.read_bytes().decode("utf-8").replace("\r\n", "\n")  # a checkout may have made the line ends CRLF
    assert text == expected


def test_the_golden_message_is_plain_ascii_with_no_hidden_character_and_ends_in_one_newline():
    data = (GOLDEN / "slack.json").read_bytes().replace(b"\r\n", b"\n")  # a checkout may have made the line ends CRLF

    assert data.isascii() and b"\r" not in data and b"\t" not in data
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")


def test_the_message_has_the_title_the_window_the_key_figures_the_first_table_the_health_and_the_link_in_that_order():
    message = slack_payload(golden_run(), link=LINK)

    kinds = [block["type"] for block in message["blocks"]]
    assert kinds == ["header", "context", "section", "section", "context", "section"]
    assert message["blocks"][0]["text"] == {"type": "plain_text", "text": "Daily sales"}
    assert message["blocks"][1]["elements"][0]["text"] == "Window 2026-07-31 00:00 to 2026-08-01 00:00, Asia/Kolkata"
    assert message["text"] == "Daily sales, 2026-07-31 00:00 to 2026-08-01 00:00, Asia/Kolkata"
    assert message["blocks"][-1]["text"]["text"] == f"<{LINK}|Open the report>"


def test_each_key_figure_is_a_field_with_its_name_its_value_and_how_it_changed():
    fields = [item["text"] for item in slack_payload(golden_run())["blocks"][2]["fields"]]

    assert fields == ["*Revenue*\n$1,234.50\n+12.2% from $1,100.00", "*Orders*\n87\n-3.3% from 90",
                      "*Share of the week*\n18.8%"]


def test_a_key_figure_that_has_a_comparison_and_no_change_says_what_it_was():
    blocks = [{"type": "kpi", "title": "Orders", "query": "q", "value": "a", "compare": "b", "format": "integer"}]
    run = make_run(blocks, {"q": pa.table({"a": [0], "b": [0]})})

    assert slack_payload(run)["blocks"][2]["fields"][0]["text"] == "*Orders*\n0\nwas 0"


def test_the_first_table_is_a_block_of_fixed_width_text_with_numbers_to_the_right_and_a_note_about_the_rows():
    text = slack_payload(golden_run())["blocks"][3]["text"]["text"]

    assert text == ("*Top cuisines*\n```\n"
                    "cuisine  orders  share\n"
                    "-------  ------  -----\n"
                    "Thai         31  35.6%\n"
                    "Italian      24  27.6%\n"
                    "Indian       17  19.5%\n"
                    "```\nShowing the first 3 of 5 rows.")


def test_the_health_is_one_line_of_what_the_panel_says():
    context = slack_payload(golden_run())["blocks"][4]

    assert context == {"type": "context", "elements": [
        {"type": "mrkdwn", "text": "Data health: 1 table is stale, gold.gold_sales"}]}


def test_a_message_without_a_link_has_no_block_for_it():
    kinds = [block["type"] for block in slack_payload(golden_run())["blocks"]]

    assert kinds == ["header", "context", "section", "section", "context"]


def test_a_link_has_its_ampersand_escaped_and_its_bar_written_as_a_percent_code_so_it_stays_one_link():
    link = "https://example.com/r?a=1&b=2|x"

    assert slack_payload(golden_run(), link=link)["blocks"][-1]["text"]["text"] == (
        "<https://example.com/r?a=1&amp;b=2%7Cx|Open the report>")


def test_a_value_that_slack_would_read_as_a_mention_or_a_link_is_shown_and_never_acted_on():
    blocks = [{"type": "kpi", "title": HOSTILE, "query": "q", "value": "v", "compare": "w"},
              {"type": "table", "title": HOSTILE, "query": "t"}]
    tables = {"q": pa.table({"v": [1], "w": [2]}), "t": pa.table({HOSTILE: [HOSTILE], "n": [1]})}
    run = make_run(blocks, tables, title=HOSTILE, what=HOSTILE)

    message = slack_payload(run, link=LINK)

    header = message["blocks"][0]["text"]["text"]
    for text in texts(message)[:-1]:  # the last is the link, which is markup that ldp wrote
        if text != header:  # a header is plain text, which Slack does not read as markup
            assert "<" not in text and ">" not in text
    field = message["blocks"][2]["fields"][0]["text"]
    assert field.startswith("*&lt;!channel&gt; &lt;@U0123&gt; &lt;https://evil.example|click&gt; &amp; ")
    table = message["blocks"][3]["text"]["text"]
    assert table.count("```") == 2 and "&lt;!channel&gt;" in table  # the ticks of a value cannot close the code block
    assert message["blocks"][0]["text"]["text"] == HOSTILE[:150]  # a header is plain text, which Slack does not read


def test_a_table_of_500_rows_and_15_columns_of_long_cells_stays_under_every_limit_of_slack_and_says_what_it_cut():
    columns = {f"column_{place}": [f"value {place} " + "x" * 60 + str(row) for row in range(500)]
               for place in range(15)}
    run = make_run([{"type": "table", "title": "Wide " + "w" * 300, "query": "q", "max_rows": 10_000}],
                   {"q": pa.table(columns)})

    message = slack_payload(run)

    table = message["blocks"][2]["text"]["text"]
    assert len(table) <= notify.SLACK_MAX_SECTION and notify.slack_message(message["text"], message["blocks"])
    assert all(len(line) <= MAX_COLUMNS * 26 for line in table.splitlines()[2:-3])
    shown = int(re.search(r"Showing the first ([\d,]+) of 500 rows\.", table).group(1))
    assert 5 <= shown < 100 and "3 more columns are in the report." in table
    assert table.count("```") == 2 and table.startswith("*Wide " + "w" * 92 + "...*\n```\n")


def test_a_table_with_no_row_says_so_and_a_table_with_every_row_has_no_note():
    nothing = make_run([{"type": "table", "title": "Empty", "query": "q"}],
                       {"q": pa.table({"a": pa.array([], pa.int64())})})
    everything = make_run([{"type": "table", "title": "Two", "query": "q"}], {"q": pa.table({"a": [1, 2]})})

    assert slack_payload(nothing)["blocks"][2]["text"]["text"] == "*Empty*\n```\na\n-\n(no rows)\n```"
    assert slack_payload(everything)["blocks"][2]["text"]["text"] == "*Two*\n```\na\n-\n1\n2\n```"


def test_a_table_that_has_no_title_has_no_title_line():
    run = make_run([{"type": "table", "query": "q"}], {"q": pa.table({"a": [1]})})

    assert slack_payload(run)["blocks"][2]["text"]["text"] == "```\na\n-\n1\n```"


def test_only_the_first_table_is_in_the_message_and_a_report_with_no_table_has_none():
    tables = [{"type": "table", "title": "First", "query": "a"}, {"type": "table", "title": "Second", "query": "b"}]
    two = make_run(tables, {"a": pa.table({"x": [1]}), "b": pa.table({"y": [2]})})
    charts = make_run([{"type": "bar", "query": "a", "x": "c", "y": "n"}],
                      {"a": pa.table({"c": ["a"], "n": [1]})})

    joined = "\n".join(texts(slack_payload(two)))
    assert "First" in joined and "Second" not in joined
    assert [block["type"] for block in slack_payload(charts)["blocks"]] == ["header", "context", "context"]


def test_a_cell_that_has_a_line_break_or_a_run_of_spaces_is_one_line_with_single_spaces_and_a_long_one_is_cut():
    run = make_run([{"type": "table", "query": "q"}], {"q": pa.table({"a": ["one\ntwo   three", "y" * 60]})})

    table = slack_payload(run)["blocks"][2]["text"]["text"]

    assert "one two three" in table and "y" * 21 + "..." in table and "y" * 22 not in table


def test_thirty_key_figures_are_twenty_in_two_sections_and_a_note_says_how_many_are_left():
    blocks = [{"type": "kpi", "title": f"K{place}", "query": "q", "value": "v"} for place in range(30)]
    message = slack_payload(make_run(blocks, {"q": pa.table({"v": [1]})}))

    kinds = [block["type"] for block in message["blocks"]]
    assert kinds == ["header", "context", "section", "section", "context", "context"]
    assert [len(block["fields"]) for block in message["blocks"][2:4]] == [10, 10] and MAX_KPIS == 20
    assert message["blocks"][4]["elements"][0]["text"] == "10 more key figures are in the report."


@pytest.mark.parametrize("count, note", [(20, None), (21, "1 more key figure is in the report."),
                                         (22, "2 more key figures are in the report.")])
def test_the_note_about_key_figures_that_were_left_out_is_there_only_when_some_were(count, note):
    blocks = [{"type": "kpi", "title": f"K{place}", "query": "q", "value": "v"} for place in range(count)]
    message = slack_payload(make_run(blocks, {"q": pa.table({"v": [1]})}))

    notes = [text for text in texts(message) if "in the report" in text]
    assert notes == ([note] if note else [])


def test_a_title_of_300_characters_is_cut_in_the_header_the_field_and_the_top_text():
    title = "t" * 300
    run = make_run([{"type": "kpi", "title": title, "query": "q", "value": "v"}], {"q": pa.table({"v": [1]})},
                   title=title)

    message = slack_payload(run)

    assert len(message["blocks"][0]["text"]["text"]) == 150
    assert message["blocks"][2]["fields"][0]["text"] == "*" + "t" * 97 + "...*\n1"
    assert len(message["text"]) <= 300 and message["text"].startswith("t" * 100)


@pytest.mark.parametrize("window, line", [
    (None, "No window"), ((START, END), "Window 2026-07-31 00:00 to 2026-08-02 00:00, UTC")])
def test_the_window_line_says_no_window_when_the_run_has_none(window, line):
    run = make_run([{"type": "text", "text": "Words."}], {"q": pa.table({"v": [1]})}, window=window)

    assert slack_payload(run)["blocks"][1]["elements"][0]["text"] == line


def test_the_health_line_says_what_the_report_read_and_whether_it_has_a_rule_to_check_it_by():
    from local_data_platform.report.health import Health, TableHealth

    fresh = Health(BUILT_AT, Freshness(26.0, "warn"), (TableHealth(table="gold.a"), TableHealth(table="gold.b")))
    ungated = Health(BUILT_AT, None, (TableHealth(table="gold.a"),))
    old = TableHealth(table="gold.a", stale=True, reasons=("old",))
    older = TableHealth(table="gold.b", stale=True, reasons=("older",))
    stale = Health(BUILT_AT, Freshness(26.0, "block"), (old, older))
    none = Health(BUILT_AT, None, ())

    def line(health):
        run = make_run([{"type": "text", "text": "Words."}], {"q": pa.table({"v": [1]})}, health=health)
        return slack_payload(run)["blocks"][-1]["elements"][0]["text"]

    assert line(fresh) == "Data health: 2 tables read, and none is stale."
    assert line(ungated) == "Data health: 1 table read, and the report has no freshness rule to check them against."
    assert line(stale) == "Data health: 2 tables are stale, gold.a, gold.b"
    assert line(none) == "Data health: this report read no table."
    assert line(None) == "Data health: this report read no table."


def test_a_table_name_that_slack_would_read_as_markup_is_escaped_in_the_health_line():
    from local_data_platform.report.health import Health, TableHealth

    health = Health(BUILT_AT, Freshness(1.0, "warn"), (TableHealth(table="gold.<!here>", stale=True, reasons=("x",)),))
    run = make_run([{"type": "text", "text": "Words."}], {"q": pa.table({"v": [1]})}, health=health)

    line = slack_payload(run)["blocks"][-1]["elements"][0]["text"]
    assert line == "Data health: 1 table is stale, gold.&lt;!here&gt;"


# --------------------------------------------------------------------------- a gold table and a report that is sent

CATALOG = {"identifier": "gold", "warehouse_path": "warehouse"}
SCHEMA = pa.schema([("order_date", pa.date32()), ("city", pa.string()), ("orders", pa.int64())])
ROWS = [(dt.date(2026, 7, 31), "Pune", 3), (dt.date(2026, 7, 31), "Delhi", 1), (dt.date(2026, 8, 1), "Pune", 4)]
BY_WINDOW = "order_date >= $window_start AND order_date < $window_end"
QUERIES = {"totals": {"sql": f"SELECT sum(orders) AS orders FROM gold.gold_sales WHERE {BY_WINDOW}"},
           "by_city": {"sql": f"SELECT city, sum(orders) AS orders FROM gold.gold_sales WHERE {BY_WINDOW} "
                              "GROUP BY 1 ORDER BY 1"}}
BLOCKS = [{"type": "kpi", "title": "Orders", "query": "totals", "value": "orders", "format": "integer"},
          {"type": "table", "title": "By city", "query": "by_city"}]
PAGE = "https://example.com/reports/daily.html"
DELIVERY = {"team": {"type": "slack", "webhook_env": "SLACK_URL"},
            "hook": {"type": "webhook", "url_env": "HOOK_URL"},
            "page": {"type": "file", "path": "out/daily.html", "public_url": PAGE}}
SINK = {"sinks": [{"type": "jsonl", "path": "events.jsonl"}]}


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def event(kind: str, number: int, moment: dt.datetime, seq: int = 0, **payload):
    from local_data_platform.events import RunEvent

    return RunEvent(event_id=f"gold_sales-{number}-{kind}-{seq}", type=kind, schema_version=1, ts=moment,
                    run_id=f"gold-sales-run-{number}", attempt=1, seq=seq,
                    payload={"pipeline": "gold_sales", "table": "gold.gold_sales", **payload})


def history(lake: Path, now: dt.datetime, hours: float) -> None:
    """One run of ``gold_sales`` that published ``hours`` before ``now``, in the file of the jsonl sink."""
    started = now - dt.timedelta(hours=hours)
    second = dt.timedelta(seconds=1)
    run = [event("run.started", 1, started, mode="overwrite", publish=True),
           event("run.extracted", 1, started + second, 1, rows_read=100),
           event("run.published", 1, started + 3 * second, 3, rows_written=100, snapshot_id=1001, failures=[]),
           event("run.finished", 1, started + 4 * second, 9, status="published", rows_written=100, duration_s=4.0)]
    folder = lake / "config" / ".ldp"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.writelines(item.to_json() + "\n" for item in run)


def now_after() -> dt.datetime:
    return dt.datetime.now(UTC) + dt.timedelta(seconds=2)


@pytest.fixture
def lake(tmp_path):
    """``config/`` with the gold table ``gold_sales`` and its config, and an empty ``reports/``."""
    table = pa.table({column: [row[place] for row in ROWS] for place, column in enumerate(SCHEMA.names)},
                     schema=SCHEMA)
    Iceberg("gold_sales", {"identifier": "gold", "warehouse_path": str(tmp_path / "config" / "warehouse")}).put(table)
    write(tmp_path / "config" / "gold_sales.json", {"identifier": "gold_sales", "who": "ops", "metadata": {
        "source": {"name": "gold_sales", "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": "gold_sales", "format": "ICEBERG", "catalog": CATALOG},
        "docs": {"layer": "gold", "description": "The table gold_sales.", "grain": "one row per order"},
        "observability": {"sinks": ["jsonl"]}}})
    (tmp_path / "reports").mkdir()
    return tmp_path


def spec_of(delivery=DELIVERY, *, observability=SINK, freshness=None, **metadata) -> dict:
    spec = {"apiVersion": "ldp/v1", "kind": "Report", "identifier": "daily", "what": "Orders of the window.",
            "metadata": {"title": "Daily orders", "tables": {"config": ["../config"], "allow": ["gold.*"]},
                         "queries": dict(QUERIES), "blocks": list(BLOCKS)}}
    for key, value in (("delivery", delivery), ("observability", observability), ("freshness", freshness)):
        if value is not None:
            spec["metadata"][key] = value
    spec["metadata"].update(metadata)
    return spec


def put_report(lake: Path, **keywords) -> Path:
    return write(lake / "reports" / "daily.json", spec_of(**keywords))


@pytest.fixture
def hooks(lineage_server, monkeypatch):
    """``SLACK_URL`` and ``HOOK_URL`` point at the local server, with a path that looks like a secret. Yields the
    handler of the server, which has ``received``, ``replies``, ``status``, ``raw`` and ``content_types``."""
    url, handler = lineage_server
    monkeypatch.setenv("SLACK_URL", f"{url}/slack/{SECRET}")
    monkeypatch.setenv("HOOK_URL", f"{url}/hook/{SECRET}")
    handler.status = 200
    handler.base = url
    return handler


def send(path: Path, *extra: str) -> int:
    return main(["report", "send", str(path), "--window", WINDOW, *extra])


def lines_of(lake: Path) -> list[dict]:
    """The events that the sink of the report wrote, as dicts."""
    file = lake / "reports" / "events.jsonl"
    return [json.loads(line) for line in file.read_text().splitlines()] if file.is_file() else []


def types_of(lake: Path) -> list[str]:
    return [item["type"] for item in lines_of(lake)]


def paths(handler) -> list[str]:
    return [path for path, _, _ in handler.received]


def test_a_report_is_sent_to_a_file_a_slack_channel_and_a_webhook_and_the_command_says_so(lake, hooks, capsys):
    path = put_report(lake)

    assert send(path) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "page (file): delivered", "team (slack): delivered", "hook (webhook): delivered",
        f"daily {SPAN}: 3 delivered"]
    assert paths(hooks) == [f"/slack/{SECRET}", f"/hook/{SECRET}"] and hooks.content_types == ["application/json"] * 2
    assert (lake / "reports" / "out" / "daily.html").read_text(encoding="utf-8").startswith("<!doctype html>")


def test_the_slack_message_that_arrives_is_the_message_of_the_run_with_the_link_to_the_page(lake, hooks):
    path = put_report(lake)
    now = now_after()

    send_report(load_report(path), WINDOW, clock=lambda: now, sink=MemorySink())

    body = hooks.received[0][2]
    run = build_report(load_report(path), WINDOW, out=None, fmt="json", clock=lambda: now, sink=MemorySink()).run
    assert body == slack_payload(run, link=PAGE)
    assert body["blocks"][-1]["text"]["text"] == f"<{PAGE}|Open the report>"
    assert body["blocks"][2]["fields"][0]["text"] == "*Orders*\n8"
    assert "Pune" in body["blocks"][3]["text"]["text"] and "Delhi" in body["blocks"][3]["text"]["text"]


def test_the_webhook_gets_the_json_of_ldp_report_build_format_json_and_the_page_is_the_page_of_a_build(lake, hooks):
    path = put_report(lake)
    now = now_after()

    send_report(load_report(path), WINDOW, clock=lambda: now, sink=MemorySink())

    built = build_report(load_report(path), WINDOW, out=None, fmt="json", clock=lambda: now, sink=MemorySink())
    assert hooks.raw[1] == built.document.encode("utf-8")
    assert json.loads(hooks.raw[1])["queries"]["totals"]["data"] == [{"orders": 8}]
    page = build_report(load_report(path), WINDOW, out=None, fmt="html", clock=lambda: now, sink=MemorySink())
    assert (lake / "reports" / "out" / "daily.html").read_bytes() == page.document.encode("utf-8")


def test_a_send_is_one_run_that_has_a_delivery_for_each_destination_and_no_row_of_data(lake, hooks):
    path = put_report(lake)

    send(path)

    items = lines_of(lake)
    assert [item["type"] for item in items] == ["run.started", "report.delivered", "report.delivered",
                                                "report.delivered", "run.finished"]
    assert len({item["run_id"] for item in items}) == 1
    started, finished = items[0]["payload"], items[-1]["payload"]
    assert (started["task_kind"], started["window"], started["output"], started["force"]) == (
        "report_send", SPAN, "page, team, hook", False)
    assert started["destinations"] == ["page", "team", "hook"] and started["spec"] == "daily.json"
    finishes = (finished["status"], finished["delivered"], finished["skipped"], finished["failed"])
    assert finishes == ("succeeded", 3, 0, 0)
    text = json.dumps(items)
    assert "Pune" not in text and "Delhi" not in text and SECRET not in text and hooks.base not in text


def test_the_event_of_a_delivery_has_the_key_the_destination_the_window_and_the_versions_that_were_read(lake, hooks):
    path = put_report(lake)
    report = load_report(path)

    send(path)

    delivered = {item["payload"]["destination"]: item["payload"] for item in lines_of(lake)
                 if item["type"] == "report.delivered"}
    assert list(delivered) == ["page", "team", "hook"]  # the file first
    team = delivered["team"]
    assert team["idempotency_key"] == delivery_key("daily", (START, END), "team")
    assert (team["type"], team["window"], team["attempts"], team["link"], team["timezone"]) == (
        "slack", SPAN, 1, PAGE, "UTC")
    assert team["bytes"] == len(hooks.raw[0]) and team["tables"][0]["table"] == "gold.gold_sales"
    assert delivered["hook"]["link"] is None and delivered["page"]["link"] is None
    assert delivered["hook"]["bytes"] == len(hooks.raw[1])
    assert set(delivered_keys(report)) == {delivery_key("daily", (START, END), name) for name in DELIVERY}


def test_the_files_are_written_first_so_that_the_link_in_the_slack_message_is_to_a_page_that_is_there(
        lake, hooks, monkeypatch):
    path = put_report(lake)
    seen = []
    real = notify.post

    def spy(channel, body):
        seen.append((channel.name, (lake / "reports" / "out" / "daily.html").is_file()))
        return real(channel, body)

    monkeypatch.setattr(notify, "post", spy)

    send(path)

    assert seen == [("team", True), ("hook", True)]


def test_a_report_with_no_file_destination_has_a_slack_message_with_no_link(lake, hooks):
    path = put_report(lake, delivery={"team": DELIVERY["team"]})

    send(path)

    assert [block["type"] for block in hooks.received[0][2]["blocks"]] == ["header", "context", "section", "section",
                                                                           "context"]


def test_a_file_destination_that_has_no_public_url_gives_no_link(lake, hooks):
    path = put_report(lake, delivery={"team": DELIVERY["team"], "page": {"type": "file", "path": "out/daily.html"}})

    send(path)

    assert hooks.received[0][2]["blocks"][-1]["type"] == "context"
    assert (lake / "reports" / "out" / "daily.html").is_file()


def test_a_file_that_cannot_be_written_is_a_failed_destination_and_the_message_has_no_link_to_it(lake, hooks, capsys):
    (lake / "reports" / "out" / "daily.html").mkdir(parents=True)
    path = put_report(lake)

    assert send(path) == 1

    out = capsys.readouterr().out
    assert "page (file): not delivered, because the file could not be written (" in out
    assert "team (slack): delivered" in out and "hook (webhook): delivered" in out
    assert hooks.received[0][2]["blocks"][-1]["type"] == "context"
    assert [item["payload"]["destination"] for item in lines_of(lake) if item["type"] == "run.failed"] == ["page"]


def test_the_link_is_in_the_message_when_the_page_was_delivered_by_an_earlier_send(lake, hooks):
    path = put_report(lake)
    send(path)
    hooks.received.clear()
    key = delivery_key("daily", (START, END), "team")
    lines = [line for line in (lake / "reports" / "events.jsonl").read_text().splitlines()
             if json.loads(line)["payload"].get("idempotency_key") != key]
    (lake / "reports" / "events.jsonl").write_text("\n".join(lines) + "\n")  # forget that the slack channel got it

    assert send(path) == 0

    assert paths(hooks) == [f"/slack/{SECRET}"]
    assert hooks.received[0][2]["blocks"][-1]["text"]["text"] == f"<{PAGE}|Open the report>"


def test_the_report_sent_again_for_the_same_window_sends_nothing_and_says_when_it_was_delivered(lake, hooks, capsys):
    path = put_report(lake)
    send(path)
    capsys.readouterr()
    hooks.received.clear()

    assert send(path) == 0

    out = capsys.readouterr().out.splitlines()
    assert len(out) == 4 and hooks.received == []
    moment = r"20\d\d-\d\d-\d\d \d\d:\d\d:\d\d UTC"
    assert re.fullmatch(rf"page \(file\): already delivered at {moment}, so nothing was sent\. --force sends it again",
                        out[0])
    assert out[-1] == f"daily {SPAN}: 3 already delivered"


def test_a_send_that_skips_every_destination_does_not_run_the_report_at_all(lake, hooks, monkeypatch):
    path = put_report(lake)
    send(path)

    def refuse(*args, **keywords):
        raise AssertionError("the report was run, and nothing had to be sent")

    monkeypatch.setattr(report_deliver, "build_report", refuse)

    assert send(path) == 0


def test_the_events_of_a_send_that_skipped_everything_say_which_deliveries_they_are_and_end_skipped_duplicate(
        lake, hooks):
    path = put_report(lake)
    send(path)
    first = lines_of(lake)[0]["run_id"]

    send(path)

    items = [item for item in lines_of(lake) if item["run_id"] != first]
    assert [item["type"] for item in items] == ["run.started", "report.skipped_duplicate", "report.skipped_duplicate",
                                                "report.skipped_duplicate", "run.finished"]
    skipped = items[1]["payload"]
    assert skipped["idempotency_key"] == delivery_key("daily", (START, END), "page")
    assert skipped["delivered_run_id"] == first and skipped["destination"] == "page" and skipped["window"] == SPAN
    assert re.fullmatch(r"20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", skipped["delivered_at"])
    assert items[-1]["payload"]["status"] == "skipped_duplicate"
    assert (items[-1]["payload"]["delivered"], items[-1]["payload"]["skipped"]) == (0, 3)


def test_force_sends_again_to_every_destination_and_records_a_new_delivery_for_each(lake, hooks, capsys):
    path = put_report(lake)
    send(path)
    capsys.readouterr()
    hooks.received.clear()

    assert send(path, "--force") == 0

    assert capsys.readouterr().out.splitlines()[-1] == f"daily {SPAN}: 3 delivered" and len(hooks.received) == 2
    assert types_of(lake).count("report.delivered") == 6
    started = [item["payload"] for item in lines_of(lake) if item["type"] == "run.started"]
    assert [item["force"] for item in started] == [False, True]


def test_a_window_written_as_the_same_two_instants_in_another_way_is_the_same_delivery(lake, hooks, capsys):
    path = put_report(lake)
    send(path)
    hooks.received.clear()

    assert main(["report", "send", str(path), "--window", "2026-07-31T00:00:00+00:00/2026-08-02T00:00:00Z"]) == 0

    assert hooks.received == [] and "3 already delivered" in capsys.readouterr().out


def test_another_window_is_another_delivery(lake, hooks, capsys):
    path = put_report(lake)
    send(path)
    hooks.received.clear()

    assert main(["report", "send", str(path), "--window", "2026-08-01/2026-08-02"]) == 0

    assert len(hooks.received) == 2 and "3 delivered" in capsys.readouterr().out.splitlines()[-1]


def test_a_destination_that_failed_is_sent_to_again_and_the_ones_that_got_the_report_are_not(lake, hooks, capsys):
    path = put_report(lake)
    hooks.replies = [(400, {}, "invalid_payload")]  # the slack channel is the first to be posted to
    assert send(path) == 1
    capsys.readouterr()
    hooks.received.clear()

    assert send(path) == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("page (file): already delivered at ") and out[1] == "team (slack): delivered"
    assert out[2].startswith("hook (webhook): already delivered at ")
    assert out[-1] == f"daily {SPAN}: 1 delivered, 2 already delivered" and paths(hooks) == [f"/slack/{SECRET}"]
    finished = [item["payload"] for item in lines_of(lake) if item["type"] == "run.finished"]
    assert [item["status"] for item in finished] == ["failed", "succeeded"]
    assert (finished[1]["delivered"], finished[1]["skipped"], finished[1]["failed"]) == (1, 2, 0)


def test_a_post_that_gets_429_is_sent_again_after_the_wait_and_the_command_says_how_many_attempts(
        lake, hooks, capsys, no_retry_wait):
    path = put_report(lake)
    hooks.replies = [(429, {"Retry-After": "2"}, ""), (200, {}, "ok")]

    assert send(path) == 0

    out = capsys.readouterr().out
    assert "team (slack): delivered after 2 attempts" in out and no_retry_wait == [2.0]
    attempts = {item["payload"]["destination"]: item["payload"]["attempts"] for item in lines_of(lake)
                if item["type"] == "report.delivered"}
    assert attempts == {"page": 1, "team": 2, "hook": 1} and len(hooks.received) == 3


@pytest.mark.parametrize("status, word", [(400, "invalid_payload"), (404, "no_service")])
def test_a_slack_error_fails_at_once_with_the_text_that_slack_gave_and_the_others_are_still_sent(
        lake, hooks, capsys, no_retry_wait, status, word):
    path = put_report(lake)
    hooks.replies = [(status, {}, word)]

    assert send(path) == 1

    captured = capsys.readouterr()
    assert f"team (slack): not delivered, because it answered HTTP {status} ({word})" in captured.out
    assert "hook (webhook): delivered" in captured.out and captured.out.splitlines()[-1] == (
        f"daily {SPAN}: 2 delivered, 1 not delivered")
    assert no_retry_wait == [] and len(hooks.received) == 2  # no retry, and the webhook got its post
    failed = [item["payload"] for item in lines_of(lake) if item["type"] == "run.failed"]
    assert failed == [{"pipeline": "daily", "report": "daily", "stage": "deliver", "destination": "team",
                       "type": "slack", "idempotency_key": delivery_key("daily", (START, END), "team"),
                       "error_type": "DeliveryError", "message": f"it answered HTTP {status} ({word})",
                       "attempts": 1}]
    assert lines_of(lake)[-1]["payload"]["status"] == "failed"


def test_a_destination_that_keeps_getting_a_5xx_answer_is_tried_four_times_and_is_not_recorded_as_delivered(
        lake, hooks, capsys, no_retry_wait):
    path = put_report(lake, delivery={"team": DELIVERY["team"]})
    hooks.status = 502

    assert send(path) == 1

    assert "team (slack): not delivered after 4 attempts, because it answered HTTP 502" in capsys.readouterr().out
    assert len(hooks.received) == 4 and no_retry_wait == [1.0, 2.0, 4.0]
    assert delivered_keys(load_report(path)) == {}


@pytest.mark.parametrize("retries, attempts", [(0, 1), (1, 2), (2, 3)])
def test_the_retries_of_a_destination_say_how_often_a_post_is_sent_again(lake, hooks, retries, attempts):
    delivery = {"team": {**DELIVERY["team"], "retries": retries}}
    path = put_report(lake, delivery=delivery)
    hooks.status = 503

    assert send(path) == 1

    assert len(hooks.received) == attempts


def test_an_address_that_is_not_in_the_environment_is_a_failed_destination_that_names_the_variable(
        lake, hooks, monkeypatch, capsys):
    path = put_report(lake)
    monkeypatch.delenv("SLACK_URL")

    assert send(path) == 1

    out = capsys.readouterr().out
    assert "team (slack): not delivered, because the environment variable SLACK_URL is not set" in out
    assert paths(hooks) == [f"/hook/{SECRET}"]


def test_a_plain_http_address_that_is_not_the_machine_itself_is_refused_and_never_posted_to(
        lake, hooks, monkeypatch, capsys):
    path = put_report(lake)
    monkeypatch.setenv("HOOK_URL", f"http://hooks.example.com/services/{SECRET}")

    assert send(path) == 1

    captured = capsys.readouterr()
    plain = "its URL is plain http, which is only for 127.0.0.1 and localhost"
    assert f"hook (webhook): not delivered, because {plain}" in captured.out
    assert paths(hooks) == [f"/slack/{SECRET}"] and SECRET not in captured.out + captured.err


def test_an_address_that_cannot_be_reached_is_a_failed_destination_and_the_reason_holds_no_address(
        lake, hooks, monkeypatch, capsys):
    path = put_report(lake)
    monkeypatch.setenv("HOOK_URL", f"http://127.0.0.1:9/hook/{SECRET}")

    assert send(path) == 1

    captured = capsys.readouterr()
    assert "hook (webhook): not delivered, because it could not be reached (ConnectionError)" in captured.out
    assert SECRET not in captured.out + captured.err and "127.0.0.1:9" not in captured.out + captured.err


def test_the_address_of_a_destination_is_in_no_event_no_log_no_error_and_no_output(
        lake, hooks, monkeypatch, capsys, caplog):
    path = put_report(lake)
    hooks.replies = [(400, {}, "invalid_payload"), (500, {}, "")] + [(500, {}, "")] * 3
    monkeypatch.setenv("HOOK_URL", f"{hooks.base}/hook/{SECRET}")

    with caplog.at_level(logging.DEBUG, logger="local_data_platform"):
        assert send(path) == 1
        assert send(path, "--force", "--dry-run") == 0
    captured = capsys.readouterr()
    everything = "\n".join([captured.out, captured.err, caplog.text, (lake / "reports" / "events.jsonl").read_text()])

    assert SECRET not in everything and hooks.base not in everything and "127.0.0.1" not in everything


def test_a_dry_run_prints_each_payload_and_sends_and_records_and_writes_nothing(lake, hooks, capsys):
    path = put_report(lake)

    assert send(path, "--dry-run") == 0

    captured = capsys.readouterr()
    assert hooks.received == [] and not (lake / "reports" / "out").exists()
    assert not (lake / "reports" / "events.jsonl").exists()
    out = captured.out
    assert re.search(r"^page \(file\): would send \d{1,3}(,\d{3})+ bytes to the file out/daily\.html$", out, re.M)
    assert re.search(r"^team \(slack\): would send \d+ bytes to SLACK_URL, which is set$", out, re.M)
    assert re.search(r"^hook \(webhook\): would send \d{1,3}(,\d{3})+ bytes to HOOK_URL, which is set$", out, re.M)
    assert '{\n  "text":' in out and "\n\n" not in out  # a payload is indented, and no line is left empty
    assert '"type": "header"' in out and '"ldp_report": 1' in out and "Open the report" in out
    assert out.splitlines()[-1] == (f"daily {SPAN}: dry run, 3 would be sent and 0 already delivered, and nothing "
                                    "was sent or recorded")
    assert SECRET not in out + captured.err and hooks.base not in out + captured.err


def test_a_dry_run_says_what_a_real_send_would_find_wrong_with_an_address_without_showing_it(
        lake, hooks, monkeypatch, capsys):
    path = put_report(lake)
    monkeypatch.delenv("SLACK_URL")
    monkeypatch.setenv("HOOK_URL", f"http://hooks.example.com/{SECRET}")

    assert send(path, "--dry-run") == 0

    out = capsys.readouterr().out
    assert "team (slack): would send" in out and "to SLACK_URL, which is not set" in out
    assert "to HOOK_URL, which is set, but its URL is plain http, which is only for 127.0.0.1 and localhost" in out
    assert SECRET not in out


def test_a_dry_run_after_a_send_says_that_the_report_was_delivered_and_with_force_it_says_it_would_send(
        lake, hooks, capsys):
    path = put_report(lake)
    send(path)
    capsys.readouterr()

    send(path, "--dry-run")
    first = capsys.readouterr().out
    send(path, "--dry-run", "--force")
    second = capsys.readouterr().out

    assert "already delivered at " in first and "would send" not in first
    assert first.splitlines()[-1] == (f"daily {SPAN}: dry run, 0 would be sent and 3 already delivered, and "
                                      "nothing was sent or recorded")
    assert ": already delivered at " not in second and second.count("would send") == 3
    assert len(hooks.received) == 2  # only the first, real send posted


def test_a_report_that_the_gate_holds_back_sends_nothing_and_exits_with_3(lake, hooks, capsys):
    now = now_after()
    history(lake, now, 40)
    path = put_report(lake, freshness={"max_age_hours": 26, "on_stale": "block"})

    assert send(path) == 3

    captured = capsys.readouterr()
    assert hooks.received == [] and not (lake / "reports" / "out").exists() and captured.out == ""
    assert "the report daily was held, because 1 table is stale and metadata.freshness says block" in captured.err


def test_a_held_send_emits_report_held_and_finishes_blocked_quality_and_ldp_runs_lists_it(lake, hooks, capsys):
    history(lake, now_after(), 40)
    path = put_report(lake, freshness={"max_age_hours": 26, "on_stale": "block"})

    send(path)
    capsys.readouterr()

    items = lines_of(lake)
    assert [item["type"] for item in items] == ["run.started", "report.held", "run.finished"]
    held, finished = items[1]["payload"], items[2]["payload"]
    assert (held["format"], held["output"], held["window"]) == ("send", "page, team, hook", SPAN)
    assert held["tables"][0]["table"] == "gold.gold_sales" and held["rule"] == {"max_age_hours": 26.0,
                                                                                "on_stale": "block"}
    assert finished["status"] == "blocked_quality"
    assert main(["runs", str(path)]) == 0
    listing = capsys.readouterr().out
    assert "blocked_quality" in listing and "page, team, hook" in listing and SPAN in listing


def test_a_report_that_the_gate_only_warns_about_is_sent_and_the_message_says_which_table_is_stale(lake, hooks):
    history(lake, now_after(), 40)
    path = put_report(lake, freshness={"max_age_hours": 26, "on_stale": "warn"})

    assert send(path) == 0

    assert hooks.received[0][2]["blocks"][-2]["elements"][0]["text"] == "Data health: 1 table is stale, gold.gold_sales"


def test_a_held_send_still_says_which_destinations_had_the_report_already(lake, hooks):
    path = put_report(lake)
    send(path)
    history(lake, now_after(), 40)
    put_report(lake, freshness={"max_age_hours": 26, "on_stale": "block"})
    key = delivery_key("daily", (START, END), "hook")
    lines = [line for line in (lake / "reports" / "events.jsonl").read_text().splitlines()
             if json.loads(line)["payload"].get("idempotency_key") != key]
    (lake / "reports" / "events.jsonl").write_text("\n".join(lines) + "\n")
    first = lines_of(lake)[0]["run_id"]
    hooks.received.clear()

    assert send(path) == 3

    later = [item["type"] for item in lines_of(lake) if item["run_id"] != first]
    assert later == ["run.started", "report.skipped_duplicate", "report.skipped_duplicate", "report.held",
                     "run.finished"] and hooks.received == []


def test_a_report_with_no_sink_is_sent_and_says_that_it_cannot_remember_it_and_so_sends_again(
        lake, hooks, capsys, caplog):
    path = put_report(lake, observability=None)

    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        assert send(path) == 0
        assert send(path) == 0

    assert len(hooks.received) == 4
    warning = "The report daily has no sink in metadata.observability, so this delivery is not recorded"
    assert caplog.text.count(warning) == 2
    assert not (lake / "reports" / "events.jsonl").exists() and not (lake / "reports" / ".ldp").exists()
    assert "already delivered" not in capsys.readouterr().out


def test_a_dry_run_of_a_report_with_no_sink_has_nothing_to_warn_about(lake, hooks, caplog):
    path = put_report(lake, observability=None)

    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        assert send(path, "--dry-run") == 0

    assert "no sink" not in caplog.text


def test_a_send_is_one_run_in_the_list_of_runs_with_the_window_and_the_destinations(lake, hooks, capsys):
    path = put_report(lake)
    send(path)
    send(path)
    capsys.readouterr()

    assert main(["runs", str(path)]) == 0

    listing = capsys.readouterr().out
    assert listing.count("succeeded") == 1 and listing.count("skipped_duplicate") == 1
    assert listing.count("page, team, hook") == 2 and listing.count(SPAN) == 2


def test_the_events_of_one_send_are_listed_with_what_each_one_was(lake, hooks, capsys):
    path = put_report(lake)
    send(path)
    run = lines_of(lake)[0]["run_id"]
    capsys.readouterr()

    assert main(["runs", str(path), "--run", run]) == 0

    out = capsys.readouterr().out
    assert "task_kind=report_send" in out
    assert re.search(r"report\.delivered\s+destination=page type=file attempts=1 key=[0-9a-f]{12}", out)
    assert re.search(r"report\.delivered\s+destination=team type=slack attempts=1", out)
    assert re.search(r"run\.finished\s+status=succeeded", out)


def test_a_report_that_has_no_delivery_says_so_and_sends_nothing(lake, hooks):
    path = put_report(lake, delivery=None)

    with pytest.raises(ConfigError, match="the report daily has no metadata.delivery, so there is nowhere to send it"):
        send_report(load_report(path), WINDOW)

    assert main(["report", "send", str(path), "--window", WINDOW]) == 1


def test_a_send_needs_a_window_because_the_window_is_part_of_the_key(lake, hooks, capsys):
    path = put_report(lake)

    with pytest.raises(ConfigError, match="ldp report send needs a window, because a report is delivered once for "):
        send_report(load_report(path), None)

    assert main(["report", "send", str(path)]) == 1
    assert "needs a window" in capsys.readouterr().err and hooks.received == []


def test_a_window_that_is_malformed_and_a_path_that_is_not_a_report_are_errors_and_send_nothing(lake, hooks, capsys):
    path = put_report(lake)

    assert main(["report", "send", str(path), "--window", "soon"]) == 1
    assert main(["report", "send", str(lake / "reports"), "--window", WINDOW]) == 1
    assert main(["report", "send", str(lake / "missing.json"), "--window", WINDOW]) == 1

    assert hooks.received == [] and "must be an ISO-8601 interval" in capsys.readouterr().err


def test_a_report_whose_query_fails_sends_nothing_and_the_send_ends_failed_at_the_queries(lake, hooks, capsys):
    path = put_report(lake, blocks=[{"type": "kpi", "query": "totals", "value": "nope"}])

    assert send(path) == 1

    assert "the report daily has 1 problem and was not built" in capsys.readouterr().err and hooks.received == []
    items = lines_of(lake)
    assert [item["type"] for item in items] == ["run.started", "run.failed", "run.finished"]
    assert (items[1]["payload"]["stage"], items[1]["payload"]["problems"]) == ("queries", 1)
    assert items[2]["payload"]["status"] == "failed"


def test_a_missing_engine_is_an_error_and_the_send_ends_failed(lake, hooks, monkeypatch):
    path = put_report(lake)

    def missing(*args, **keywords):
        raise EngineNotFound("duckdb is not installed")

    monkeypatch.setattr(report_deliver, "build_report", missing)

    with pytest.raises(EngineNotFound):
        send_report(load_report(path), WINDOW, sink=(sink := MemorySink()))

    assert sink.types == ["run.started", "run.failed", "run.finished"] and hooks.received == []


def test_the_build_of_a_send_records_no_run_of_its_own(lake, hooks):
    path = put_report(lake)

    send(path)

    assert "report.rendered" not in types_of(lake) and len({item["run_id"] for item in lines_of(lake)}) == 1


def test_the_options_of_the_command_are_handed_to_the_build_and_nothing_else(lake, hooks, monkeypatch, capsys):
    path = put_report(lake)
    calls = []

    def record(report, window=None, **keywords):
        calls.append((window, keywords))
        raise ReportBuildError(report.identifier, ["stop here"])

    monkeypatch.setattr(report_deliver, "build_report", record)

    assert main(["report", "send", str(path), "--window", "yesterday", "--param", "city=Delhi", "--release", "r1",
                 "--row-cap", "7", "--timeout", "9"]) == 1
    assert main(["report", "send", str(path), "--window", WINDOW, "--live", "--force"]) == 1

    capsys.readouterr()
    window, keywords = calls[0]
    assert window == "yesterday" and keywords["fmt"] == "json" and keywords["out"] is None
    assert (keywords["params"], keywords["release"], keywords["live"], keywords["row_cap"], keywords["timeout_s"]) == (
        {"city": "Delhi"}, "r1", False, 7, 9.0)
    assert keywords["sink"].__class__.__name__ == "NullSink"  # the build of a send records no run of its own
    assert (calls[1][1]["params"], calls[1][1]["release"], calls[1][1]["live"]) == (None, None, True)
    assert keywords["now"] is not None and keywords["clock"] is not None


def test_the_help_of_the_command_says_what_it_sends_and_what_the_exit_status_means(capsys):
    assert main(["report", "send", "--help"]) == 0

    text = " ".join(capsys.readouterr().out.split())
    assert "--window WINDOW" in text and "--dry-run" in text and "--force" in text
    assert "idempotency key made from the report, the window and the destination" in text
    assert "Exit status is 0 when every destination got the report or had it, 1 when a destination did not" in text
    assert "3 when the freshness gate of the report held it back" in text
    assert main(["report"]) == 1 and "send" in capsys.readouterr().out


def test_the_outcomes_of_a_send_have_the_status_word_of_each_destination(lake, hooks):
    path = put_report(lake)

    sent = send_report(load_report(path), WINDOW)
    again = send_report(load_report(path), WINDOW)
    forced = send_report(load_report(path), WINDOW, force=True)

    assert [item.status for item in sent.outcomes] == [DELIVERED] * 3 and sent.window == SPAN
    assert sent.delivered == sent.outcomes and sent.skipped == () and sent.failed == ()
    assert [item.status for item in again.outcomes] == [SKIPPED] * 3 and again.skipped == again.outcomes
    assert all(item.delivered_at is not None for item in again.outcomes)
    assert [item.status for item in forced.outcomes] == [DELIVERED] * 3
    assert {DELIVERED, SKIPPED, FAILED, WOULD_SEND} == {"delivered", "skipped", "failed", "would_send"}


def test_a_send_through_the_api_has_the_events_in_the_sink_that_it_is_given_and_not_in_the_sink_of_the_report(
        lake, hooks):
    path = put_report(lake)
    sink = MemorySink()

    send_report(load_report(path), WINDOW, sink=sink)

    assert sink.types == ["run.started", "report.delivered", "report.delivered", "report.delivered", "run.finished"]
    assert not (lake / "reports" / "events.jsonl").exists() and sink.flushes == 1


def test_a_destination_is_sent_to_with_the_clock_that_is_given_for_the_events_and_for_the_window_shortcuts(lake, hooks):
    path = put_report(lake)
    moment = dt.datetime(2026, 8, 2, 1, 0, tzinfo=UTC)
    sink = MemorySink()

    sent = send_report(load_report(path), "yesterday", clock=lambda: moment, sink=sink)

    assert sent.window == "2026-08-01T00:00:00Z/2026-08-02T00:00:00Z"
    assert {item.ts for item in sink.events} == {moment}


def test_the_delivery_of_a_report_that_has_a_time_zone_is_made_once_for_the_window_in_utc(lake, hooks):
    path = put_report(lake, timezone="Asia/Kolkata")

    sent = send_report(load_report(path), "2026-07-31T00:00:00+05:30/2026-08-01T00:00:00+05:30", sink=MemorySink())

    assert sent.window == "2026-07-30T18:30:00Z/2026-07-31T18:30:00Z"
    assert sent.outcomes[0].key == delivery_key("daily", (dt.datetime(2026, 7, 30, 18, 30, tzinfo=UTC),
                                                          dt.datetime(2026, 7, 31, 18, 30, tzinfo=UTC)), "page")


def test_the_headers_of_a_webhook_destination_are_sent_with_the_post(lake, hooks, monkeypatch):
    delivery = {"hook": {"type": "webhook", "url_env": "HOOK_URL", "headers_env": "HOOK_HEADERS"}}
    path = put_report(lake, delivery=delivery)
    monkeypatch.setenv("HOOK_HEADERS", json.dumps({"Authorization": "Bearer abc-123"}))

    assert send(path) == 0

    assert hooks.received[0][1] == "Bearer abc-123"


def test_a_message_that_cannot_be_made_fails_that_destination_alone_and_the_others_are_sent(
        lake, hooks, monkeypatch, capsys):
    path = put_report(lake)

    def refuse(run, *, link=None):
        raise ValueError("block 3 is a section of 3001 characters, and the limit is 3000")

    monkeypatch.setattr(report_deliver, "slack_payload", refuse)

    assert send(path) == 1

    out = capsys.readouterr().out
    assert "team (slack): not delivered, because its payload could not be made (ValueError)" in out
    assert "page (file): delivered" in out and "hook (webhook): delivered" in out
    assert paths(hooks) == [f"/hook/{SECRET}"]
    failed = [item["payload"] for item in lines_of(lake) if item["type"] == "run.failed"]
    assert [(item["stage"], item["destination"], item["error_type"]) for item in failed] == [
        ("payload", "team", "ValueError")]
    assert failed[0]["message"] == "block 3 is a section of 3001 characters, and the limit is 3000"


def test_the_latest_delivery_of_a_key_is_the_one_that_is_kept_and_the_other_events_are_left_alone(lake):
    from local_data_platform.events import RunEvent

    path = put_report(lake)
    key = "k" * 64

    def line(kind, moment, **payload):
        return RunEvent(event_id=str(uuid.uuid4()), type=kind, schema_version=1, ts=moment, run_id=f"run-{moment.hour}",
                        attempt=1, seq=0, payload={"pipeline": "daily", **payload}).to_json()

    at = [dt.datetime(2026, 8, 1, hour, tzinfo=UTC) for hour in (6, 7, 8)]
    (lake / "reports" / "events.jsonl").write_text("\n".join([
        line("report.delivered", at[1], idempotency_key=key), line("report.delivered", at[2], idempotency_key=key),
        line("report.delivered", at[0], idempotency_key=key),
        line("report.skipped_duplicate", at[2], idempotency_key="s" * 64), line("run.finished", at[2], status="x"),
        line("report.delivered", at[2]), line("report.delivered", at[2], idempotency_key=7),
        line("report.delivered", at[2], pipeline="weekly", idempotency_key="w" * 64)]) + "\n")

    found = delivered_keys(load_report(path))

    assert set(found) == {key} and found[key].ts == at[2] and found[key].run_id == "run-8"


def test_a_report_with_no_events_has_no_delivered_keys_and_nothing_is_created_by_looking(lake):
    path = put_report(lake)

    assert delivered_keys(load_report(path)) == {} and not (lake / "reports" / "events.jsonl").exists()
    assert not (lake / "reports" / ".ldp").exists()


def test_a_file_destination_can_be_a_file_uri_and_the_page_is_there(lake, hooks, tmp_path):
    target = tmp_path / "shared drive" / "daily.html"
    path = put_report(lake, delivery={"page": {"type": "file", "path": target.as_uri()}})

    assert send(path) == 0

    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")


@pytest.fixture(scope="module")
def moto_endpoint():
    server_module = pytest.importorskip("moto.server", reason="moto[server] is not installed")
    werkzeug = logging.getLogger("werkzeug")
    level = werkzeug.level
    werkzeug.setLevel(logging.ERROR)
    server = server_module.ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.stop()
        werkzeug.setLevel(level)


def test_a_page_can_be_delivered_to_a_bucket_and_the_dry_run_says_which_object_it_would_write(
        lake, hooks, moto_endpoint, monkeypatch, tmp_path_factory, capsys):
    boto3 = pytest.importorskip("boto3", reason="boto3 is not installed")
    for name in ("AWS_ENDPOINT_URL_S3", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    nowhere = tmp_path_factory.mktemp("no-aws")
    for name, value in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"),
                        ("AWS_REGION", "us-east-1"), ("AWS_DEFAULT_REGION", "us-east-1"),
                        ("AWS_ENDPOINT_URL", moto_endpoint), ("AWS_EC2_METADATA_DISABLED", "true"),
                        ("AWS_CONFIG_FILE", str(nowhere / "config")),
                        ("AWS_SHARED_CREDENTIALS_FILE", str(nowhere / "credentials"))):
        monkeypatch.setenv(name, value)
    bucket = f"ldp-send-{uuid.uuid4().hex[:12]}"
    client = boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1")
    client.create_bucket(Bucket=bucket)
    page = {"type": "file", "path": f"s3://{bucket}/team/daily.html", "public_url": PAGE}
    path = put_report(lake, delivery={"page": page})

    assert send(path, "--dry-run") == 0
    assert "would send" in capsys.readouterr().out and "Contents" not in client.list_objects_v2(Bucket=bucket)

    assert send(path) == 0

    body = client.get_object(Bucket=bucket, Key="team/daily.html")["Body"].read()
    assert body.startswith(b"<!doctype html>") and not client.list_multipart_uploads(Bucket=bucket).get("Uploads")


# --------------------------------------------------------------------------- the table of the message, at its limits


def grid_run(columns: dict, title: str | None = None):
    block = {"type": "table", "query": "q", "max_rows": 10_000}
    if title is not None:
        block["title"] = title
    return make_run([block], {"q": pa.table(columns)})


def table_text(run) -> str:
    return slack_payload(run)["blocks"][2]["text"]["text"]


@pytest.mark.parametrize("title", [None, "&" * 200, "t" * 300])
@pytest.mark.parametrize("columns, width", [(1, 1), (1, 7), (2, 23), (3, 10), (12, 24), (15, 24), (15, 60), (2, 60)])
def test_a_table_of_any_shape_fits_in_a_section_whatever_its_title_and_the_width_of_its_cells(columns, width, title):
    data = {f"c{place:02d}" + "x" * max(0, width - 3): ["v" * width] * 600 for place in range(columns)}

    message = slack_payload(grid_run(data, title))

    assert len(message["blocks"][2]["text"]["text"]) <= notify.SLACK_MAX_SECTION
    assert notify.slack_message(message["text"], message["blocks"])


@pytest.mark.parametrize("names, cell, shown", [(1, 7, 347), (2, 23, 54)])
def test_the_rows_that_fit_are_counted_to_the_character_and_a_row_that_just_fits_is_shown(names, cell, shown):
    data = {chr(ord("a") + place) * cell: ["x" * cell] * 400 for place in range(names)}

    text = table_text(grid_run(data))

    assert text.endswith(f"\n```\nShowing the first {shown} of 400 rows.")
    assert text.count("\n") == 2 + shown + 2  # the fence, the heading, the rule, the rows, the fence and the note


def test_a_table_of_a_thousand_rows_and_more_writes_its_counts_with_separators():
    text = table_text(grid_run({"c": ["x"] * 2000}))

    assert text.endswith("```\nShowing the first 1,394 of 2,000 rows.")


def test_the_first_row_that_does_not_fit_ends_the_table_even_when_a_later_row_is_short_enough():
    data = {"h" * 23: ["x" * 23] * 200 + ["y"]}

    text = table_text(grid_run(data))

    assert "\ny\n```" not in text and "of 201 rows." in text


def test_a_tick_in_a_title_a_heading_or_a_cell_never_ends_the_block_of_code():
    data = {"`h`": ["```", "a`b"], "n": [1, 2]}

    text = table_text(grid_run(data, "`T` ```"))

    assert text == "*'T' '''*\n```\n'h'  n\n---  -\n'''  1\na'b  2\n```" and text.count("```") == 2


def test_the_columns_are_as_wide_as_their_widest_cell_numbers_are_to_the_right_and_no_line_ends_in_spaces():
    data = {"city": ["Delhi", "Pune"], "orders": [1, 10], "note": ["a", "longer note"]}

    text = table_text(grid_run(data, "By city"))

    assert text == ("*By city*\n```\n"
                    "city   orders  note\n"
                    "-----  ------  -----------\n"
                    "Delhi       1  a\n"
                    "Pune       10  longer note\n"
                    "```")
    assert not any(line.endswith(" ") for line in text.splitlines())


def test_the_note_about_rows_and_the_note_about_columns_are_one_line_and_each_is_in_the_singular_when_it_is_one():
    wide = {f"c{place:02d}" + "x" * 21: ["v" * 24] * 500 for place in range(14)}
    one_more = {f"c{place:02d}": ["v"] * 3 for place in range(13)}

    both = table_text(grid_run(wide)).splitlines()[-1]
    columns_only = table_text(grid_run(one_more)).splitlines()[-1]

    assert re.fullmatch(r"Showing the first \d+ of 500 rows\. 2 more columns are in the report\.", both), both
    assert columns_only == "1 more column is in the report."


def test_the_health_line_of_a_report_with_a_hundred_stale_tables_is_cut_to_500_characters():
    from local_data_platform.report.health import Health, TableHealth, stale_text

    tables = tuple(TableHealth(table=f"gold.table_{number:03d}", stale=True, reasons=("old",)) for number in range(100))
    run = make_run([{"type": "text", "text": "Words."}], {"q": pa.table({"v": [1]})},
                   health=Health(BUILT_AT, Freshness(1.0, "warn"), tables))

    line = slack_payload(run)["blocks"][-1]["elements"][0]["text"]

    assert line == "Data health: " + stale_text(tables)[:497] + "..." and len(line) == 13 + 500


def test_a_message_is_checked_against_the_limits_of_slack_before_it_is_sent(monkeypatch):
    monkeypatch.setattr(notify, "SLACK_MAX_SECTION", 50)

    with pytest.raises(ValueError, match=r"block 4 is a section of \d+ characters, and the limit is 50"):
        slack_payload(golden_run())


def test_the_letters_of_a_title_are_in_the_post_as_they_are_and_not_as_escapes(lake, hooks):
    path = put_report(lake, title="Caf\u00e9 orders")

    assert send(path) == 0

    assert "Caf\u00e9 orders".encode("utf-8") in hooks.raw[0] and b"\\u00e9" not in hooks.raw[0]


def test_the_window_shortcuts_count_from_the_moment_that_is_given_and_the_events_from_the_clock(lake, hooks):
    path = put_report(lake)
    counting = dt.datetime(2026, 8, 2, 1, 0, tzinfo=UTC)
    later = dt.datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    sink = MemorySink()

    sent = send_report(load_report(path), "yesterday", now=counting, clock=lambda: later, sink=sink)

    assert sent.window == "2026-08-01T00:00:00Z/2026-08-02T00:00:00Z" and {item.ts for item in sink.events} == {later}


def test_the_page_is_made_only_when_a_file_destination_has_to_be_sent(lake, hooks, monkeypatch):
    calls = []
    real = report_deliver.render
    monkeypatch.setattr(report_deliver, "render", lambda *args, **keywords: calls.append(1) or real(*args, **keywords))

    send(put_report(lake, delivery={"team": DELIVERY["team"], "hook": DELIVERY["hook"]}))
    assert calls == []

    send(put_report(lake, delivery={"page": DELIVERY["page"]}))
    assert calls == [1]


def test_the_reason_of_a_failed_delivery_is_masked_in_the_output_and_the_event_whatever_it_says(
        lake, hooks, monkeypatch, capsys):
    path = put_report(lake)
    monkeypatch.setattr(notify, "post", lambda channel, body: notify.Delivery(channel.label, False,
                                                                              "it said password=hunter2", 1))

    assert send(path) == 1

    out = capsys.readouterr().out
    assert "not delivered, because it said password=***" in out
    assert "hunter2" not in out + (lake / "reports" / "events.jsonl").read_text()


def test_the_message_of_a_payload_that_could_not_be_made_is_masked_in_its_event(lake, hooks, monkeypatch):
    path = put_report(lake)

    def refuse(run, *, link=None):
        raise ValueError("bad value password=hunter2")

    monkeypatch.setattr(report_deliver, "slack_payload", refuse)

    send(path)

    failed = [item["payload"] for item in lines_of(lake) if item["type"] == "run.failed"]
    assert failed[0]["message"] == "bad value password=***"


def test_the_message_of_an_error_that_stops_a_send_is_masked_in_its_event(lake, hooks, monkeypatch):
    path = put_report(lake)

    def stop(*args, **keywords):
        raise RuntimeError("could not connect with password=hunter2\nsecond line")

    monkeypatch.setattr(report_deliver, "build_report", stop)

    with pytest.raises(RuntimeError):
        send_report(load_report(path), WINDOW)

    failed = [item["payload"] for item in lines_of(lake) if item["type"] == "run.failed"]
    assert failed[0]["message"] == "could not connect with password=***" and failed[0]["stage"] == "queries"


def test_the_release_that_was_read_is_in_the_delivery_and_the_versions_of_the_tables(lake, hooks):
    from local_data_platform.gold.build import build_gold

    release = build_gold(lake / "config", release=True).release.release.name
    path = put_report(lake)

    assert send(path, "--release", release) == 0

    delivered = [item["payload"] for item in lines_of(lake) if item["type"] == "report.delivered"]
    assert {item["release"] for item in delivered} == {release}
    assert {table["release"] for item in delivered for table in item["tables"]} == {release}
    started = next(item["payload"] for item in lines_of(lake) if item["type"] == "run.started")
    assert started["release"] == release
