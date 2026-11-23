"""Alerts: the one notifier that tells a person or a tool when a run breaks or a table goes bad, and when it recovers.

A config lists its channels in ``metadata.observability.alerts``::

    "observability": {"alerts": [
      {"type": "slack", "url_env": "LDP_SLACK_WEBHOOK"},
      {"type": "webhook", "url_env": "ALERT_URL", "token_env": "ALERT_TOKEN", "repeat_after_minutes": 60},
      {"type": "file", "path": ".ldp/alerts.jsonl", "on": ["monitor.error", "monitor.resolved"]}
    ]}

A ``slack`` channel posts ``{"text": ...}`` to an incoming webhook, a ``webhook`` channel posts the JSON of the
``ldp.alert`` schema, version 1 (see :func:`alert_json_schema`), and a ``file`` channel appends that same JSON as one
line. The URL of a channel is never in a config. ``url_env`` names the environment variable that holds it, and
``token_env`` names the one that holds a bearer token, so a config can be committed. Each channel has ``on``, the
events that it wants, drawn from :data:`EVENTS`, and the default is :data:`DEFAULT_ON`. A channel that has
``repeat_after_minutes`` says again, that often, that a monitor is still in ``warn`` or ``error``.

There are two senders of alerts. :class:`NotifySink` is an event sink that ``sink_from_config`` adds for a config that
has channels, so ``ldp run``, a workflow task and a gold build alert when a run ends ``run.failed`` or
``run.blocked_quality``. ``ldp monitor`` alerts when a monitor changes state (see
:mod:`local_data_platform.monitoring.alerts`), with the previous state read from the history of the monitors.

An alert never holds a row of a table, a value that failed a check or the text of an error. A run alert says the stage,
the kind of error and the names of the checks that failed. A monitor alert says what the monitor observed and expected,
which are counts, times and names. A channel that is down, a URL that is not set and a file that cannot be written are
a warning in the log and a :class:`Delivery` that says why, and they never fail a run, and the reason never holds a
URL.

The same sender carries the deliveries of ``ldp report send`` (see :mod:`local_data_platform.report.deliver`), which
posts a Slack message that has Block Kit blocks and the JSON of a report to a webhook, and writes the page of a report
to a file. A post that gets HTTP 429 or a 5xx answer is sent again, up to ``retries`` times, which is :data:`RETRIES`
for an alert, after a wait that doubles each time and that an answer can set with ``Retry-After``. Any other answer,
and a failure to reach the URL, is final. A destination of a report refuses a URL that is not https, except
``http://`` for 127.0.0.1 and localhost.

This module needs only the standard library when it is imported. ``requests`` and the event code are imported when an
alert is sent.
"""

import argparse
import dataclasses
import datetime as dt
import email.utils
import json
import math
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.logger import get_logger

logger = get_logger(__name__)

ALERT_SCHEMA = "ldp.alert"
"""The name of the schema of the JSON that a webhook and a file get."""

ALERT_VERSION = 1
"""The version of the ``ldp.alert`` schema."""

CHANNEL_TYPES = ("slack", "webhook", "file")
"""The ``type`` of a channel."""

EVENTS = ("run.failed", "run.blocked_quality", "monitor.error", "monitor.warn", "monitor.resolved", "monitor.heartbeat")
"""The events that a channel can ask for with ``on``. A run ends ``run.failed`` or ``run.blocked_quality``. A monitor
goes to ``warn`` or ``error`` and comes back with ``monitor.resolved``, and ``monitor.heartbeat`` is a ping after each
completed ``ldp monitor``."""

DEFAULT_ON = ("run.failed", "run.blocked_quality", "monitor.error", "monitor.resolved")
"""The events of a channel that sets no ``on``."""

TEST_EVENT = "alert.test"
"""The event of the message that ``ldp alert test`` sends. A channel gets it whatever its ``on`` says."""

DEFAULT_FILE = ".ldp/alerts.jsonl"
"""Where a ``file`` channel appends when it names no ``path``, relative to the folder of the config."""

TIMEOUT_S = 10.0
"""How long a channel waits for an answer, in seconds."""

MAX_NAMES = 10
"""How many names of failed checks a run alert lists."""

RETRIES = 3
"""How many times a post is sent again after HTTP 429 or a 5xx answer, unless the destination says another number."""

BACKOFF_S = 1.0
"""The wait before the first retry, in seconds. Each wait is twice the one before, unless the answer sets it."""

MAX_WAIT_S = 60.0
"""The longest that a retry waits. An answer that asks for more than this is not retried."""

LOCAL_HOSTS = ("127.0.0.1", "localhost")
"""The only hosts that a channel that wants https may reach over plain ``http://``."""

SLACK_MAX_BLOCKS = 50
SLACK_MAX_SECTION = 3000
SLACK_MAX_FIELDS = 10
SLACK_MAX_FIELD = 2000
SLACK_MAX_HEADER = 150
SLACK_MAX_CONTEXT = 10
"""What Slack allows in a message: blocks, characters of a section and of a field, fields in a section, characters of a
header and elements of a context block. A message over a limit is refused with ``invalid_blocks``."""

CHANNEL_KEYS = {
    "slack": ("type", "url_env", "on", "repeat_after_minutes"),
    "webhook": ("type", "url_env", "token_env", "on", "repeat_after_minutes"),
    "file": ("type", "path", "on", "repeat_after_minutes"),
}
"""The keys of each type of channel."""

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_SLACK_ERROR = re.compile(r"[a-z0-9_]{1,64}")
_RUN_EVENTS = ("run.failed", "run.blocked_quality")
_HEADLINES = {
    "run.failed": "run failed", "run.blocked_quality": "run blocked by a quality check",
    "monitor.error": "monitor error", "monitor.warn": "monitor warning", "monitor.resolved": "monitor resolved",
    "monitor.heartbeat": "monitor ran", TEST_EVENT: "alert test",
}


# ---------------------------------------------------------------------- the channels in a config


def _whole(value: Any, least: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= least


def _env_problem(value: Any, example: str) -> str | None:
    """Why ``value`` is no name of an environment variable, or ``None``. A URL is never repeated back."""
    if isinstance(value, str) and "://" in value:
        return ("is a URL, and it has to be the name of an environment variable such as "
                f"{example}, so the URL stays out of the config")
    if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
        from local_data_platform.events import redact_text

        return f"must be the name of an environment variable such as {example}, got {redact_text(repr(value), 80)}"
    return None


def _channel_problems(channel: Any, where: str) -> list[tuple[str, str]]:
    if not isinstance(channel, Mapping):
        return [(where, f"must be an object with a type, such as {{'type': 'slack', 'url_env': 'LDP_SLACK_WEBHOOK'}}, "
                        f"got {channel!r}")]
    kind = channel.get("type")
    if "type" not in channel:
        return [(f"{where}.type", f"is required, one of {list(CHANNEL_TYPES)}")]
    if kind not in CHANNEL_TYPES:
        return [(f"{where}.type", f"must be one of {list(CHANNEL_TYPES)}, got {kind!r}")]
    allowed = CHANNEL_KEYS[kind]
    problems: list[tuple[str, str]] = []
    unknown = sorted(str(key) for key in channel if key not in allowed)
    if unknown:
        problems.append((where, f"has unknown keys {unknown}; allowed: {list(allowed)}"))
    if kind in ("slack", "webhook"):
        if "url_env" not in channel:
            problems.append((f"{where}.url_env", "is required, the name of the environment variable with the URL"))
        elif (message := _env_problem(channel["url_env"], "LDP_SLACK_WEBHOOK")) is not None:
            problems.append((f"{where}.url_env", message))
    if kind == "webhook" and "token_env" in channel and (message := _env_problem(channel["token_env"],
                                                                                 "ALERT_TOKEN")) is not None:
        problems.append((f"{where}.token_env", message))
    if kind == "file" and "path" in channel and not (isinstance(channel["path"], str) and channel["path"].strip()):
        problems.append((f"{where}.path", f"must be a file name, got {channel['path']!r}"))
    if "on" in channel:
        events = channel["on"]
        if not isinstance(events, (list, tuple)) or not events:
            problems.append((f"{where}.on", f"must be a list of at least one of {list(EVENTS)}, got {events!r}"))
        else:
            for position, event in enumerate(events):
                if event not in EVENTS:
                    problems.append((f"{where}.on[{position}]", f"must be one of {list(EVENTS)}, got {event!r}"))
            known = [event for event in events if event in EVENTS]
            if len(set(known)) != len(known):
                problems.append((f"{where}.on", "lists an event twice"))
    if "repeat_after_minutes" in channel and not _whole(channel["repeat_after_minutes"], 1):
        problems.append((f"{where}.repeat_after_minutes",
                         f"must be a whole number of minutes from 1, got {channel['repeat_after_minutes']!r}"))
    return problems


def alert_problems(alerts: Any) -> list[tuple[str, str]]:
    """Every problem of the ``alerts`` list of ``metadata.observability``, as ``(path, message)``.

    Args:
        alerts: The value of ``metadata.observability.alerts``. ``None`` is no list, and has no problem.

    Returns:
        The problems, which are empty when the list is fine. The path is relative to the list, so it is ``""`` for the
        list, ``"[1]"`` for a channel and ``"[1].url_env"`` for a key of a channel.
    """
    if alerts is None:
        return []
    if not isinstance(alerts, (list, tuple)):
        return [("", f"must be a list of channels such as {{'type': 'slack', 'url_env': 'LDP_SLACK_WEBHOOK'}}, "
                     f"got {alerts!r}")]
    problems: list[tuple[str, str]] = []
    for index, channel in enumerate(alerts):
        problems.extend(_channel_problems(channel, f"[{index}]"))
    return problems


@dataclasses.dataclass(frozen=True)
class Channel:
    """One place that alerts go to.

    Attributes:
        kind: ``slack``, ``webhook`` or ``file``.
        on: The events that the channel wants.
        repeat_after_minutes: How often a monitor that stays in ``warn`` or ``error`` is said again, or ``None``.
        url_env: The environment variable that holds the URL of a ``slack`` or ``webhook`` channel.
        token_env: The environment variable that holds the bearer token of a ``webhook`` channel, or ``None``.
        path: The file of a ``file`` channel, relative to the folder of the config.
        name: What a destination of a report is called, or ``None`` for a channel of alerts.
        headers_env: The environment variable that holds a JSON object of the headers of a ``webhook``, or ``None``.
        retries: How many times a post is sent again after HTTP 429 or a 5xx answer.
        https_only: Whether the URL has to be https, with ``http://127.0.0.1`` and ``http://localhost`` as the only
            exceptions.
    """

    kind: str
    on: tuple[str, ...] = DEFAULT_ON
    repeat_after_minutes: int | None = None
    url_env: str | None = None
    token_env: str | None = None
    path: str | None = None
    name: str | None = None
    headers_env: str | None = None
    retries: int = RETRIES
    https_only: bool = False

    @property
    def label(self) -> str:
        """The channel in a few words that name no secret, such as ``slack LDP_SLACK_WEBHOOK``, or ``slack team`` for a
        destination called ``team``."""
        if self.name:
            return f"{self.kind} {self.name}"
        return f"file {self.path or DEFAULT_FILE}" if self.kind == "file" else f"{self.kind} {self.url_env}"

    def wants(self, event: str) -> bool:
        """Whether the channel asked for ``event``."""
        return event in self.on


def parse_alerts(metadata: Mapping[str, Any]) -> tuple[Channel, ...]:
    """The channels of ``metadata.observability.alerts``, with the defaults applied.

    Args:
        metadata: The ``metadata`` of a config.

    Returns:
        The channels in the order of the list, and none when there is no list.

    Raises:
        ConfigError: If the list is not valid. The message names the first problem, as
            ``metadata.observability.alerts[0].url_env: ...``.
    """
    block = metadata.get("observability") if isinstance(metadata, Mapping) else None
    raw = block.get("alerts") if isinstance(block, Mapping) else None
    problems = alert_problems(raw)
    if problems:
        path, message = problems[0]
        raise ConfigError(f"metadata.observability.alerts{path}: {message}")
    return tuple(Channel(kind=item["type"], on=tuple(item.get("on", DEFAULT_ON)),
                         repeat_after_minutes=item.get("repeat_after_minutes"), url_env=item.get("url_env"),
                         token_env=item.get("token_env"), path=item.get("path") if item["type"] == "file" else None)
                 for item in raw or ())


def alerts_schema() -> dict[str, Any]:
    """The JSON Schema of the ``alerts`` list of ``metadata.observability``."""
    env = {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"}
    shared = {
        "on": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": list(EVENTS)},
               "default": list(DEFAULT_ON), "description": "The events that the channel wants."},
        "repeat_after_minutes": {"type": "integer", "minimum": 1,
                                 "description": "How often a monitor that stays in warn or error is said again."},
    }

    def channel(kind: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False, "required": ["type", *required],
                "properties": {"type": {"const": kind}, **properties, **shared}}

    return {
        "type": "array",
        "description": "Where to tell a person or a tool when a run fails, is blocked by a quality check, or a monitor "
                       "breaks and recovers. A URL is never in the config, only the name of the environment variable "
                       "that holds it.",
        "items": {"oneOf": [
            channel("slack", {"url_env": {**env, "description": "The environment variable with the webhook URL."}},
                    ["url_env"]),
            channel("webhook", {"url_env": {**env, "description": "The environment variable with the URL."},
                                "token_env": {**env, "description": "The environment variable with a bearer token."}},
                    ["url_env"]),
            channel("file", {"path": {"type": "string", "minLength": 1, "default": DEFAULT_FILE,
                                      "description": "The file that one line of JSON is appended to for each alert."}},
                    []),
        ]},
    }


# ---------------------------------------------------------------------- an alert


def _moment(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclasses.dataclass(frozen=True)
class Alert:
    """One message for a channel. It holds names, counts and times and never a row.

    Attributes:
        event: One of :data:`EVENTS`, or :data:`TEST_EVENT`.
        at: When it happened, which is the end of the run or the moment that ``ldp monitor`` measured.
        pipeline: The config that it is about.
        table: Its table.
        owner: The ``who`` of the config.
        command: What to run to look closer, such as ``ldp status config/orders.json``.
        facts: What the event says, by key. See :func:`alert_json_schema`.
    """

    event: str
    at: dt.datetime
    pipeline: str | None = None
    table: str | None = None
    owner: str | None = None
    command: str | None = None
    facts: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The alert as the JSON of the ``ldp.alert`` schema, version 1, without the keys that it has no value for."""
        body = {"schema": ALERT_SCHEMA, "version": ALERT_VERSION, "event": self.event, "at": _moment(self.at),
                "pipeline": self.pipeline, "table": self.table, "owner": self.owner, "command": self.command,
                **self.facts}
        return {key: value for key, value in body.items() if value is not None}

    def text(self) -> str:
        """The alert as the text of a Slack message: the pipeline, table and owner, what was seen, and what to run."""
        facts = self.facts
        where = ", ".join(part for part in (f"table {self.table}" if self.table else "",
                                            f"owner {self.owner}" if self.owner else "") if part)
        again = " again" if facts.get("repeat") else ""
        lines = [f"ldp {_HEADLINES.get(self.event, self.event)}{again}"
                 + (f" on {self.pipeline}" if self.pipeline else "") + (f" ({where})" if where else "")]
        if self.event == "run.failed":
            lines.append(", ".join(part for part in (f"stage {facts['stage']}" if facts.get("stage") else "",
                                                     facts.get("error_type") or "", f"attempt {facts.get('attempt')}")
                                   if part))
        elif self.event == "run.blocked_quality":
            count = facts.get("checks_failed")
            names = ", ".join(facts.get("failures") or [])
            lines.append(f"{count} check{'' if count == 1 else 's'} failed" + (f": {names}" if names else ""))
        elif self.event.startswith("monitor.") and self.event != "monitor.heartbeat":
            if self.event == "monitor.resolved":
                lines.append(f"{facts.get('monitor')} is {facts.get('status')} again, "
                             f"it was {facts.get('previous_status')}")
            lines.append(f"{facts.get('monitor')}: observed {facts.get('observed') or '-'}, "
                         f"expected {facts.get('expected') or '-'}")
            if facts.get("details"):
                lines.append(str(facts["details"]))
        elif self.event == "monitor.heartbeat":
            counts = facts.get("results") or {}
            lines.append("results: " + (", ".join(f"{number} {status}" for status, number in counts.items()) or "none"))
        elif facts.get("message"):
            lines.append(str(facts["message"]))
        if self.command:
            lines.append(f"Look closer with `{self.command}`")
        return "\n".join(lines)


def alert_json_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) of the body of a webhook and of a line of a file, ``ldp.alert`` version 1.

    Unknown keys are allowed, so a newer producer stays readable by an older consumer. A key that an event has no value
    for is left out.
    """
    text = {"type": "string"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ldp.alert",
        "type": "object",
        "required": ["schema", "version", "event", "at"],
        "properties": {
            "schema": {"const": ALERT_SCHEMA}, "version": {"const": ALERT_VERSION},
            "event": {"enum": [*EVENTS, TEST_EVENT]},
            "at": {"type": "string", "format": "date-time",
                   "description": "When it happened, the end of the run or the moment ldp monitor measured."},
            "pipeline": text, "table": text, "owner": text, "command": text,
            "monitor": {**text, "description": "A monitor event. The name of the monitor, such as freshness."},
            "kind": text, "status": {"enum": ["ok", "warn", "error", "unknown"]},
            "previous_status": {"enum": ["ok", "warn", "error"]},
            "observed": text, "expected": text, "details": text, "evaluated_at": text,
            "repeat": {"type": "boolean", "description": "True when the same state is said again."},
            "results": {"type": "object", "additionalProperties": {"type": "integer"},
                        "description": "A heartbeat. How many monitors are in each status."},
            "monitors": {"type": "integer"},
            "run_id": text, "attempt": {"type": "integer"}, "stage": text, "error_type": text,
            "checks_failed": {"type": "integer"}, "failures": {"type": "array", "items": text},
            "workflow": text, "task_id": text, "message": text,
        },
        "additionalProperties": True,
    }


# ---------------------------------------------------------------------- sending


@dataclasses.dataclass(frozen=True)
class Delivery:
    """What happened when an alert or a report was sent to a channel.

    Attributes:
        channel: The label of the channel.
        ok: Whether it was sent.
        reason: Why not, in a few words. It never holds a URL or the text of an exception.
        attempts: How many times the post was made, which is 1 unless it was sent again.
    """

    channel: str
    ok: bool
    reason: str = ""
    attempts: int = 1


def url_problem(url: str) -> str | None:
    """Why a URL may not be posted to by a channel that wants https, or ``None`` when it may.

    A URL has to be ``https://``. Plain ``http://`` is only for 127.0.0.1 and localhost, which is where the tests and a
    local stub listen. The reason never holds the URL.
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return "its URL is not a valid address"
    if not host:
        return "its URL is not a valid address"
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOCAL_HOSTS):
        return None
    if parts.scheme == "http":
        return "its URL is plain http, which is only for 127.0.0.1 and localhost"
    return "its URL does not start with https://"


def _sleep(seconds: float) -> None:
    """Wait before a retry. Tests replace it, so that no test waits."""
    time.sleep(seconds)


def _retry_after(value: str | None) -> float | None:
    """The seconds that a ``Retry-After`` header asks for, as a number or a date, or ``None`` when it asks nothing."""
    if not value or not value.strip():
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = (when - dt.datetime.now(when.tzinfo or dt.timezone.utc)).total_seconds()
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _wait_s(attempt: int, response: Any) -> float | None:
    """How long to wait before the retry that follows attempt number ``attempt``, or ``None`` for no retry.

    The answer can ask for a wait with ``Retry-After``, and it is honoured, so an answer that asks for more than
    :data:`MAX_WAIT_S` ends the retries. Otherwise the wait is :data:`BACKOFF_S`, twice that, and so on.
    """
    headers = getattr(response, "headers", None) or {}
    asked = _retry_after(headers.get("Retry-After"))
    if asked is None:
        asked = BACKOFF_S * 2 ** (attempt - 1)
    return asked if asked <= MAX_WAIT_S else None


def _answer_reason(code: int, response: Any, slack: bool) -> str:
    """Why an answer was not good, as ``it answered HTTP 400``. Slack says what is wrong in the body, as one word such
    as ``invalid_payload``, and that word is added. No other body is ever repeated."""
    reason = f"it answered HTTP {code}"
    if slack and 400 <= code < 500 and code != 429:
        word = str(getattr(response, "text", "") or "").strip()
        if _SLACK_ERROR.fullmatch(word):
            reason += f" ({word})"
    return reason


def _post(label: str, url: str, headers: Mapping[str, str], body: Mapping[str, Any] | bytes, *,
          retries: int = RETRIES, slack: bool = False) -> Delivery:
    import requests

    data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    attempts = 0
    while True:
        attempts += 1
        try:
            response = requests.post(url, data=data, timeout=TIMEOUT_S,
                                     headers={**headers, "Content-Type": "application/json"})
        except requests.Timeout:
            return Delivery(label, False, f"it did not answer in {TIMEOUT_S:g} seconds", attempts)
        except requests.RequestException as error:
            return Delivery(label, False, f"it could not be reached ({type(error).__name__})", attempts)
        code = response.status_code
        if code < 300:
            return Delivery(label, True, "", attempts)
        if (code == 429 or 500 <= code <= 599) and attempts <= retries:
            wait = _wait_s(attempts, response)
            if wait is None:
                return Delivery(label, False, f"it answered HTTP {code} and asked for a wait of more than "
                                              f"{MAX_WAIT_S:g} seconds", attempts)
            logger.debug("%s answered HTTP %s, so the post is sent again in %.1f seconds (attempt %d of %d)", label,
                         code, wait, attempts + 1, retries + 1)
            _sleep(wait)
            continue
        return Delivery(label, False, _answer_reason(code, response, slack), attempts)


def _headers_of(name: str) -> tuple[dict[str, str], str | None]:
    """The headers in the environment variable ``name``, a JSON object, and why they cannot be used, or ``None``.

    The problem never holds a header value. A ``Content-Type`` is dropped, because a post is always JSON.
    """
    value = os.environ.get(name)
    if not value:
        return {}, f"the environment variable {name} is not set"
    try:
        found = json.loads(value)
    except ValueError:
        found = None
    if not isinstance(found, dict) or not found:
        return {}, f"the environment variable {name} does not hold a JSON object of headers"
    headers: dict[str, str] = {}
    for key, item in found.items():
        if not (isinstance(key, str) and _HEADER_NAME.fullmatch(key) and isinstance(item, str)
                and not re.search(r"[\r\n\x00]", item)):
            return {}, f"a header in the environment variable {name} is not a name and a text without line breaks"
        if key.lower() != "content-type":
            headers[key] = item
    return headers, None


def post(channel: Channel, body: Mapping[str, Any] | bytes) -> Delivery:
    """Post one JSON body to a ``slack`` or ``webhook`` channel. It never raises, and the reason holds no URL.

    The URL is in the environment variable that the channel names. A channel that wants https refuses another URL
    before anything is sent. A post that gets HTTP 429 or a 5xx answer is sent again up to ``channel.retries`` times.

    Args:
        channel: Where to post.
        body: A mapping, which is written as JSON, or the bytes of JSON that is already written.

    Returns:
        The :class:`Delivery`.
    """
    if channel.kind not in ("slack", "webhook"):
        raise ValueError(f"post sends to a slack or a webhook channel, and this is a {channel.kind} channel")
    url = os.environ.get(channel.url_env or "")
    if not url:
        return Delivery(channel.label, False, f"the environment variable {channel.url_env} is not set")
    if channel.https_only:
        problem = url_problem(url)
        if problem is not None:
            return Delivery(channel.label, False, problem)
    headers: dict[str, str] = {}
    if channel.kind == "webhook" and channel.token_env:
        token = os.environ.get(channel.token_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            logger.warning("Alert channel %s: the environment variable %s is not set, so it is sent without a token",
                           channel.label, channel.token_env)
    if channel.kind == "webhook" and channel.headers_env:
        extra, problem = _headers_of(channel.headers_env)
        if problem is not None:
            return Delivery(channel.label, False, problem)
        headers.update(extra)
    return _post(channel.label, url, headers, body, retries=channel.retries, slack=channel.kind == "slack")


def write_file(label: str, location: str | Path, data: bytes, base_dir: str | Path | None = None) -> Delivery:
    """Write a document, such as the page of a report, to a path or a ``file://``, ``s3://`` or ``gs://`` address.

    The file is written whole or not at all, and it replaces the one that was there. It never raises.

    Args:
        label: The label of the destination.
        location: Where to write. A relative path is relative to ``base_dir``.
        data: The bytes.
        base_dir: The folder that a relative path is relative to.
    """
    from local_data_platform import fs

    try:
        with fs.open_output_atomic(str(location), base_dir) as stream:
            stream.write(data)
    except Exception as error:  # noqa: BLE001 - a delivery says why it failed and never raises
        return Delivery(label, False, f"the file could not be written ({type(error).__name__})")
    return Delivery(label, True)


def deliver(channel: Channel, alert: Alert, base_dir: str | Path | None = None) -> Delivery:
    """Send an alert to one channel. It never raises, and the reason of a failure holds no URL.

    Args:
        channel: Where to send it.
        alert: What to send.
        base_dir: The folder that the path of a ``file`` channel is relative to.

    Returns:
        The :class:`Delivery`.
    """
    if channel.kind == "file":
        from local_data_platform.events import append_line
        from local_data_platform.paths import resolve_path

        try:
            append_line(resolve_path(channel.path or DEFAULT_FILE, base_dir),
                        json.dumps(alert.to_dict(), sort_keys=True, ensure_ascii=False))
        except (OSError, ValueError) as error:
            return Delivery(channel.label, False, f"the file could not be written ({type(error).__name__})")
        return Delivery(channel.label, True)
    return post(channel, {"text": alert.text()} if channel.kind == "slack" else alert.to_dict())


# ---------------------------------------------------------------------- Slack messages


def escape_mrkdwn(text: Any) -> str:
    """Text that Slack shows as it is. Slack reads ``&``, ``<`` and ``>`` as markup, so a value that has ``<!channel>``
    or a link in it would ping people or send them away, unless the three are written as entities."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cut(text: str, limit: int) -> str:
    """The text, or its first characters and ``...`` when it is longer than ``limit``, and never longer than that."""
    if len(text) <= limit:
        return text
    return text[:limit] if limit <= 3 else text[: limit - 3] + "..."


def header_block(text: str) -> dict[str, Any]:
    """A header block, which is plain text of at most :data:`SLACK_MAX_HEADER` characters. A longer text is cut."""
    return {"type": "header", "text": {"type": "plain_text", "text": cut(text, SLACK_MAX_HEADER)}}


def section_block(text: str | None = None, fields: Sequence[str] | None = None) -> dict[str, Any]:
    """A section block with a text of mrkdwn, or with fields, or both. The caller escapes the text with
    :func:`escape_mrkdwn`. :func:`slack_message` checks the limits."""
    block: dict[str, Any] = {"type": "section"}
    if text is not None:
        block["text"] = {"type": "mrkdwn", "text": text}
    if fields:
        block["fields"] = [{"type": "mrkdwn", "text": field} for field in fields]
    return block


def context_block(*texts: str) -> dict[str, Any]:
    """A context block, small text under the rest, with one element of mrkdwn for each text."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text} for text in texts]}


def slack_message(text: str, blocks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The body of a Slack message that has Block Kit blocks, after checking what Slack allows.

    Args:
        text: The text that a notification and a client that cannot show blocks use.
        blocks: The blocks, from :func:`header_block`, :func:`section_block` and :func:`context_block`.

    Returns:
        ``{"text": text, "blocks": [...]}``.

    Raises:
        ValueError: If a limit of :data:`SLACK_MAX_BLOCKS` and the others is passed, which Slack would answer with
            ``invalid_blocks``.
    """
    if not blocks or len(blocks) > SLACK_MAX_BLOCKS:
        raise ValueError(f"a message has from 1 to {SLACK_MAX_BLOCKS} blocks, and this has {len(blocks)}")
    for place, block in enumerate(blocks, 1):
        kind = block.get("type")
        shown = block.get("text", {}).get("text", "") if isinstance(block.get("text"), Mapping) else ""
        if kind == "header" and len(shown) > SLACK_MAX_HEADER:
            raise ValueError(f"block {place} is a header of {len(shown)} characters, and the limit is "
                             f"{SLACK_MAX_HEADER}")
        if kind == "section":
            fields = block.get("fields") or []
            if len(shown) > SLACK_MAX_SECTION:
                raise ValueError(f"block {place} is a section of {len(shown)} characters, and the limit is "
                                 f"{SLACK_MAX_SECTION}")
            if len(fields) > SLACK_MAX_FIELDS or any(len(item["text"]) > SLACK_MAX_FIELD for item in fields):
                raise ValueError(f"block {place} has more than {SLACK_MAX_FIELDS} fields or a field of more than "
                                 f"{SLACK_MAX_FIELD} characters")
        if kind == "context" and (len(block["elements"]) > SLACK_MAX_CONTEXT
                                  or any(len(item["text"]) > SLACK_MAX_FIELD for item in block["elements"])):
            raise ValueError(f"block {place} is a context of more than {SLACK_MAX_CONTEXT} elements or an element of "
                             f"more than {SLACK_MAX_FIELD} characters")
    return {"text": text, "blocks": list(blocks)}


# ---------------------------------------------------------------------- the notifier


def _short(text: Any, limit: int) -> str | None:
    from local_data_platform.events import redact_text

    return redact_text(text, limit) if text is not None else None


@dataclasses.dataclass
class Notifier:
    """The channels of one config, and what an alert of it says about the config.

    Attributes:
        channels: Where alerts go.
        pipeline: The identifier of the config.
        table: Its target table.
        owner: Its ``who``.
        command: What to run to look closer, or ``None``.
        base_dir: The folder of the config, which a relative file path of a channel is relative to.
    """

    channels: tuple[Channel, ...]
    pipeline: str | None = None
    table: str | None = None
    owner: str | None = None
    command: str | None = None
    base_dir: Path | None = None

    def alert(self, event: str, at: dt.datetime, **facts: Any) -> Alert:
        """An alert of this config, without the facts that are ``None``."""
        return Alert(event=event, at=at, pipeline=self.pipeline, table=self.table, owner=self.owner,
                     command=self.command, facts={key: value for key, value in facts.items() if value is not None})

    def run_alert(self, event: Any) -> Alert:
        """The alert for a ``run.failed`` or ``run.blocked_quality`` event. It has the stage, the kind of error and the
        names of the failed checks, and never the message of an error."""
        payload = event.payload
        facts: dict[str, Any] = {"run_id": event.run_id, "attempt": event.attempt}
        if event.type == "run.failed":
            facts.update(stage=_short(payload.get("stage"), 100), error_type=_short(payload.get("error_type"), 100))
        else:
            facts.update(checks_failed=payload.get("checks_failed"),
                         failures=[_short(name, 100) for name in list(payload.get("failures") or [])[:MAX_NAMES]])
        for key in ("workflow", "task_id"):
            if payload.get(key):
                facts[key] = _short(payload[key], 200)
        found = self.alert(event.type, event.ts, **facts)
        return dataclasses.replace(found, pipeline=_short(payload.get("pipeline"), 200) or self.pipeline,
                                   table=_short(payload.get("table"), 200) or self.table)

    def monitor_alert(self, result: Any, event: str, previous: str | None, repeat: bool = False) -> Alert:
        """The alert for a monitor that went to ``warn`` or ``error`` or came back to ``ok``.

        Args:
            result: The :class:`~local_data_platform.monitoring.results.MonitorResult`.
            event: ``monitor.warn``, ``monitor.error`` or ``monitor.resolved``.
            previous: The last known status of the monitor before this one, or ``None`` for its first look.
            repeat: Whether it says again a state that the monitor was in already.
        """
        return self.alert(event, result.evaluated_at, monitor=_short(result.monitor, 200),
                          kind=_short(result.kind, 100), status=result.status, previous_status=previous,
                          observed=_short(result.observed, 200), expected=_short(result.expected, 200),
                          details=_short(result.details, 500), repeat=True if repeat else None,
                          evaluated_at=_moment(result.evaluated_at))

    def heartbeat_alert(self, results: Sequence[Any], at: dt.datetime) -> Alert:
        """The ping after a completed ``ldp monitor``, with how many monitors are in each status."""
        counts: dict[str, int] = {}
        for result in results:
            counts[result.status] = counts.get(result.status, 0) + 1
        return self.alert("monitor.heartbeat", at, results=counts, monitors=len(results))

    def send(self, channel: Channel, alert: Alert) -> Delivery:
        """Send an alert to a channel, and log a warning when it does not arrive. It never raises."""
        try:
            delivery = deliver(channel, alert, self.base_dir)
        except Exception as error:  # noqa: BLE001 - an alert must not fail what it is about
            delivery = Delivery(channel.label, False, f"it failed ({type(error).__name__})")
        if not delivery.ok:
            logger.warning("The %s alert for %s was not sent to %s, because %s", alert.event,
                           self.pipeline or "a config", channel.label, delivery.reason)
        return delivery

    def broadcast(self, alert: Alert) -> list[Delivery]:
        """Send an alert to every channel that wants its event."""
        return [self.send(channel, alert) for channel in self.channels if channel.wants(alert.event)]

    def test(self, at: dt.datetime | None = None) -> list[Delivery]:
        """Send one test message to every channel, whatever its ``on`` says."""
        moment = at or dt.datetime.now(dt.timezone.utc)
        message = (f"This is a test message from ldp alert test for {self.pipeline or 'a config'}. "
                   "Nothing is wrong.")
        return [self.send(channel, self.alert(TEST_EVENT, moment, message=message)) for channel in self.channels]


def command_of(config: Any) -> str | None:
    """``ldp status`` for the file of a config, with the path relative to the working folder when it is inside it."""
    path = getattr(config, "source_path", None)
    if path is None:
        return None
    path = Path(path).resolve()
    try:
        path = path.relative_to(Path.cwd())  # the working folder is a real path, so only the file needs resolving
    except ValueError:
        pass
    return f"ldp status {path}"


def notifier_for(config: Any) -> Notifier | None:
    """The notifier of a config, or ``None`` when its ``metadata.observability`` lists no channel.

    Raises:
        ConfigError: If the list of channels is not valid.
    """
    channels = parse_alerts(config.metadata)
    if not channels:
        return None
    from local_data_platform.events import redact_text

    return Notifier(channels=channels, pipeline=config.identifier, table=_target_table(config),
                    owner=redact_text(config.who, 200) if config.who else None, command=command_of(config),
                    base_dir=Path(config.base_dir) if config.base_dir is not None else None)


def _target_table(config: Any) -> str | None:
    """The table of the target of a config, as ``namespace.name`` for an Iceberg table, without opening anything."""
    block = config.metadata.get("target")
    if not isinstance(block, Mapping):
        return None
    if str(block.get("format", "")).strip().upper() == "ICEBERG" and isinstance(block.get("catalog"), Mapping):
        from local_data_platform.catalog.provider import catalog_namespace

        try:
            return f"{catalog_namespace(block['catalog'])}.{block['name']}"
        except Exception:  # noqa: BLE001 - a table that cannot be named is left out of the alert
            return None
    return str(block.get("path") or block.get("name") or "") or None


class NotifySink:
    """An event sink that sends an alert when a run ends ``run.failed`` or ``run.blocked_quality``.

    It holds the two events until the run is flushed, which a run does once at its end, and then sends an alert for
    each to the channels that want it. It passes nothing on and writes nothing, and a channel that is down is a warning,
    so it cannot fail a run. A workflow task that is retried alerts for each attempt that fails, because the sink cannot
    know that another attempt follows.

    Args:
        notifier: The channels and what an alert says about the config.
    """

    def __init__(self, notifier: Notifier):
        self.notifier = notifier
        self._pending: list[Any] = []
        self._lock = threading.Lock()

    def emit(self, event: Any) -> None:
        """Hold the event when it is one that ends a run badly."""
        if event.type in _RUN_EVENTS:
            with self._lock:
                self._pending.append(event)

    def flush(self) -> None:
        """Send the alerts of the run that just ended."""
        with self._lock:
            pending, self._pending = self._pending, []
        for event in pending:
            self.notifier.broadcast(self.notifier.run_alert(event))

    def __repr__(self) -> str:
        return f"NotifySink({[channel.label for channel in self.notifier.channels]!r})"


# ---------------------------------------------------------------------- ldp alert test


def _cmd_test(args: argparse.Namespace) -> int:
    from local_data_platform.cli import EXIT_ERROR, EXIT_OK
    from local_data_platform.config import Config

    config = Config.from_json(args.config)
    notifier = notifier_for(config)
    if notifier is None:
        print(f"{config.identifier} has no channel in metadata.observability.alerts, so there is nothing to test")
        return EXIT_ERROR
    deliveries = notifier.test()
    for delivery in deliveries:
        print(f"{delivery.channel}: " + ("sent" if delivery.ok else f"not sent, because {delivery.reason}"))
    sent = sum(1 for delivery in deliveries if delivery.ok)
    print(f"{sent} of {len(deliveries)} channels got the test message")
    return EXIT_OK if sent == len(deliveries) else EXIT_ERROR


def add_cli(subparsers: Any, parents: Sequence[argparse.ArgumentParser] = ()) -> None:
    """Register ``ldp alert`` on an ``argparse`` subparsers object.

    Args:
        subparsers: What ``ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers to inherit options from (e.g. the CLI's ``-v``).
    """
    alert = subparsers.add_parser(
        "alert", parents=list(parents), help="try the alert channels of a config",
        description="Work with the alert channels of metadata.observability.alerts.",
        epilog="Exit status is 0 when every channel got the message and 1 otherwise.")
    actions = alert.add_subparsers(dest="alert_command", metavar="COMMAND")

    def usage(args: argparse.Namespace) -> int:
        alert.print_help()
        return 1

    alert.set_defaults(handler=usage)
    test = actions.add_parser(
        "test", parents=list(parents), help="send one test message to each channel of a config",
        description="Send one test message to each channel of CONFIG, whatever its on says, and print which ones got "
                    "it. A slack or webhook channel posts to the URL in its environment variable, and a file channel "
                    "appends a line, so this sends real messages.",
        epilog="Exit status is 0 when every channel got the message and 1 otherwise. Example: ldp alert test "
               "config/orders.json")
    test.add_argument("config", metavar="CONFIG", help="a JSON dataset config that has alert channels")
    test.set_defaults(handler=_cmd_test)


__all__ = [
    "ALERT_SCHEMA",
    "ALERT_VERSION",
    "Alert",
    "BACKOFF_S",
    "CHANNEL_KEYS",
    "CHANNEL_TYPES",
    "Channel",
    "DEFAULT_FILE",
    "DEFAULT_ON",
    "Delivery",
    "EVENTS",
    "LOCAL_HOSTS",
    "MAX_WAIT_S",
    "NotifySink",
    "Notifier",
    "RETRIES",
    "SLACK_MAX_BLOCKS",
    "SLACK_MAX_CONTEXT",
    "SLACK_MAX_FIELD",
    "SLACK_MAX_FIELDS",
    "SLACK_MAX_HEADER",
    "SLACK_MAX_SECTION",
    "TEST_EVENT",
    "add_cli",
    "alert_json_schema",
    "alert_problems",
    "alerts_schema",
    "command_of",
    "context_block",
    "cut",
    "deliver",
    "escape_mrkdwn",
    "header_block",
    "notifier_for",
    "parse_alerts",
    "post",
    "section_block",
    "slack_message",
    "url_problem",
    "write_file",
]
