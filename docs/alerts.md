# Alerts to Slack, a webhook or a file

A config can tell a person or a tool when a run fails, when a quality check blocks it and when a table breaks its SLA, and
tell them again when it recovers, so that nobody has to open a dashboard to notice. The channels are listed in
`metadata.observability.alerts`. One notifier, `local_data_platform.notify`, sends everything, and nothing needs a service
of its own.

```json
"observability": {
  "alerts": [
    {"type": "slack", "url_env": "LDP_SLACK_WEBHOOK"},
    {"type": "webhook", "url_env": "ALERT_URL", "token_env": "ALERT_TOKEN",
     "on": ["monitor.error", "monitor.resolved", "monitor.heartbeat"], "repeat_after_minutes": 60},
    {"type": "file", "path": ".ldp/alerts.jsonl"}
  ]
}
```

## The channels

| Type | What it does | Keys |
|---|---|---|
| `slack` | Posts `{"text": ...}` to a Slack incoming webhook | `url_env` |
| `webhook` | Posts the JSON of [the `ldp.alert` schema](#the-message) to a URL, with a bearer token when `token_env` is set | `url_env`, `token_env` |
| `file` | Appends the same JSON as one line to a file, which is relative to the folder of the config | `path`, `.ldp/alerts.jsonl` by default |

Every channel can also have `on` and `repeat_after_minutes`. The URL of a channel is never in a config, because a config is
meant to be committed. `url_env` names the environment variable that holds it, and `token_env` names the one that holds a
bearer token. `ldp plan` reports a config that has a Slack webhook URL in any of its values as an inline secret, and an error message
that quotes one shows `https://hooks.slack.com/services/***`. `ldp plan` checks the channels, and `ldp schema` describes
them.

## The events

`on` is the list of events that a channel wants. A channel that sets none gets the ones marked in the table.

| Event | When it is sent | By default |
|---|---|---|
| `run.failed` | A run ends failed. `ldp run`, a task of a workflow and a gold build send it | yes |
| `run.blocked_quality` | A quality check or a contract held a run back, and nothing was written | yes |
| `monitor.error` | A monitor of `ldp monitor` goes to `error` | yes |
| `monitor.warn` | A monitor goes to `warn` | no |
| `monitor.resolved` | A monitor that was alerting is not any more | yes |
| `monitor.heartbeat` | `ldp monitor` has finished looking at the config | no |

## Runs

A config that has channels gets a notifying sink next to the sinks that it names, so every way of running it alerts. When a
run ends, the sink sends one alert for a `run.failed` or `run.blocked_quality` event to each channel that wants it. A run
that publishes, and one that finds its idempotency key already published, send nothing. A task of a workflow that is
retried sends an alert for each attempt that fails, because the sink cannot know that another attempt follows.

A config that lists only `alerts` still gets the `iceberg` sink that a gold build adds for a config that names no sinks.

## Monitors

`ldp monitor` alerts for a change of state and not for a state, so a table that is late for a day does not send a message
every time that it is looked at. Each channel sees a monitor through its own `on`. It sees the monitor as alerting while it
is in a status that the channel wants, which is `error` for a channel that has `monitor.error` and `warn` as well for one
that has `monitor.warn`.

| What happens to a monitor | What the channel sends |
|---|---|
| It becomes alerting, or goes from one alerting status to another | The alert for its new status. A monitor that was never looked at counts as `ok`, so a table that is broken at the first look alerts |
| It stops being alerting | `monitor.resolved`, when the channel wants it |
| It stays in the same alerting status | Nothing, unless the channel has `repeat_after_minutes` |
| It says `unknown` | Nothing, and the alert that is open stays open |

The previous state of a monitor comes from the history of the monitors, as its last result that is not `unknown`, so there is
no file of alert state. `ldp monitor` reads it before it records the new results, records them, and then sends. A look at
a moment before results that are in the history does not see them, so `--now` can go back in time.

With `repeat_after_minutes` a state that lasts is said again. The alert goes out again at the first look after each
multiple of that many minutes, counted from the first look that saw the state, marked `repeat` in its JSON and with `again` in
its text. It is said once for each stretch of minutes, also after a long gap between two looks. An alert that did not arrive is not sent
again at the next look, so a channel that was down gets it back when the next repeat is due.

`monitor.heartbeat` is a ping after each look at a config that completed, whatever the monitors found. A dead man's switch
outside, such as Healthchecks.io, can raise an alarm when the pings stop, which is what a laptop that sleeps or a scheduler
that died look like. A config whose look failed sends none, and a config that has no `metadata.sla` is not looked at.

## The message

A Slack message is `{"text": ...}` and names the pipeline, its table and its owner, which is the `who` of the config. A
monitor alert says the monitor, what it observed and what it expected, and each alert ends with the command to look closer.

```text
ldp monitor error on orders (table shop.orders, owner ops team)
freshness: observed 30.0 hour, expected error after 26 hour
the last commit was 30.0 hour ago, at 2026-11-17T14:52:28Z
Look closer with `ldp status config/orders.json`
```

A webhook and a file get JSON of the `ldp.alert` schema, version 1, and a line of a file is that JSON on one line. Keys that an
event has no value for are left out, and a consumer has to ignore keys that it does not know. `alert_json_schema()` in
`local_data_platform.notify` returns the JSON Schema.

| Key | Meaning |
|---|---|
| `schema`, `version` | `ldp.alert` and `1` |
| `event` | One of the events above, or `alert.test` for `ldp alert test` |
| `at` | When it happened, in UTC. For a run that is the end of the run, and for a monitor the moment that `ldp monitor` measured at |
| `pipeline`, `table`, `owner` | The identifier of the config, its target table and its `who` |
| `command` | What to run to look closer, such as `ldp status config/orders.json`. It is left out for a config that was not read from a file |
| `monitor`, `kind`, `status`, `previous_status`, `observed`, `expected`, `details`, `evaluated_at` | A monitor event. The monitor, its family, its status now and before, what it saw and what it expected, and one sentence |
| `repeat` | `true` when the alert says again a state that was said already |
| `results`, `monitors` | A heartbeat. How many monitors are in each status, and how many there are |
| `run_id`, `attempt`, `stage`, `error_type`, `checks_failed`, `failures`, `workflow`, `task_id` | A run event. The run and its attempt, where it failed and the kind of error, how many checks failed and their names, and the workflow and task of a workflow run |
| `message` | The text of an `alert.test` message |

## What is never in an alert

An alert has names, counts and times. It has no row of a table and no value that failed a check. A run alert says the stage and
the kind of error and leaves the message of the error out, because such a message can quote a value of a row, and `ldp runs`
shows it. A blocked run lists the names of the checks that failed, and the first 10 of them. The text that a monitor alert
carries is cut and goes through the same masking of secrets as the events of a run.

## Trying a channel

```bash
ldp alert test config/orders.json
```

The command sends one test message to each channel of the config, whatever its `on` says, and prints which ones got it. It
sends real messages, so a Slack channel posts and a file channel appends a line with the event `alert.test`. The exit status is
0 when every channel got it and 1 otherwise.

## When a channel is down

A channel that cannot be reached, that answers with an error status, whose environment variable is not set or whose file cannot
be written is a warning in the log and nothing more. A run is not failed by it, a monitor keeps its exit status, and the
results are recorded. The reason names the environment variable or the kind of failure and never holds the URL, so a log can
be shared. A token that is not set is a warning too, and the alert is sent without it.
