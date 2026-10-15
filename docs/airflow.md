# Run a config from Apache Airflow 3

**You will learn** how to run an LDP config as an Airflow 3 task, how the data interval, the tries and the backfills of Airflow become the windows, the attempts and the keys of LDP, and how to do the same with a BashOperator when LDP cannot live in the environment of Airflow.

**It takes** about 30 minutes. Most of it is Airflow starting and one retry waiting for its delay.

**You need** Python 3.12 or newer, a clone of this repo, and a few gigabytes of free memory. Airflow runs on macOS and Linux, and on Windows through WSL2. The data comes from the restaurant example in `examples/restaurant`, so nothing is downloaded after the install and there is no account.

## Why run LDP from Airflow

Airflow decides when a task runs, how often it is retried and which days a backfill covers. LDP decides whether a day is already in the table. They meet in three values.

| Airflow | LDP | What it gives |
|---|---|---|
| `data_interval_start` and `data_interval_end` of the DAG run | The window | The idempotency key hashes the pipeline, the target and the window, so it is the key that `ldp plan CONFIG --window START/END` prints for the same interval |
| `ti.try_number`, which is 1 for the first try | The attempt | The first commit of try N fences every older try of the task, also one that Airflow gave up on and that is still running |
| The DAG id, the run id of the DAG run and the task id | The run id `airflow:DAG_ID:RUN_ID:TASK_ID` | Every try of a task has the same run id, and no two tasks share one |

So a retry carries on the run that failed, a backfill over a day that is in the table finds the key and writes nothing, and a day is loaded once however Airflow got to it. The [exactly-once guide](exactly_once.md) explains the key, the fence and the retries.

`LdpRunOperator` is the task. The module `local_data_platform.integrations.airflow` also has the mapping as plain functions, for a `@task` or a PythonOperator, and none of it needs Airflow to be installed until you ask for the operator.

## Install

LDP and Airflow share one virtual environment, so Airflow's constraints file has to accept LDP's dependencies. Airflow publishes one constraints file for each release and each Python version, and its installation guide asks for it with every install, because a plain `pip install apache-airflow` can end in an unusable Airflow. Install Airflow first and LDP second, with the same file. Everything this guide creates goes into `ldp_demo/airflow`, and git ignores `ldp_demo`, so you can delete the folder to start again.

```bash
mkdir -p ldp_demo/airflow
python3 -m venv ldp_demo/airflow/venv
. ldp_demo/airflow/venv/bin/activate
AIRFLOW_VERSION=3.3.2
PYTHON_VERSION="$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
CONSTRAINT_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"
pip install "apache-airflow==${AIRFLOW_VERSION}" --constraint "${CONSTRAINT_URL}"
pip install -e . --constraint "${CONSTRAINT_URL}"
pip check
```

`pip check` prints `No broken requirements found.` The second install runs from the root of the clone. There is no `airflow` extra of LDP, because Airflow has to come with its own constraints file and pip extras cannot carry one.

**Which Airflow.** Use 3.1.7 or newer. The constraints file of every Airflow 3 release for Python 3.12 was resolved together with LDP. The files of 3.0.0 to 3.0.2 pin pyarrow 16.1.0, and LDP needs 17 or newer. The files of 3.0.3 to 3.0.6 pin pyiceberg 0.9.1, and LDP needs 0.11 or newer. The files of 3.1.0 to 3.1.6 pin SQLAlchemy 1.4, and the SQL catalog of pyiceberg needs 2.0.18 or newer. From 3.1.7 on, pip finds a set that works. The run in this guide used 3.3.2. If you are stuck on an older Airflow 3, keep LDP in a virtual environment of its own and use [the BashOperator recipe](#without-importing-ldp-the-bashoperator-recipe), which only needs the path of the `ldp` command.

## Start Airflow on your laptop

`airflow standalone` starts everything Airflow needs in one terminal. It makes the database, a user, the API server, the scheduler, the DAG processor and the triggerer. The database is a SQLite file under `AIRFLOW_HOME`, so give this guide a home of its own. `AIRFLOW__CORE__LOAD_EXAMPLES=False` keeps the example DAGs of Airflow out of the list. Run these commands from the root of the clone, with the virtual environment active.

```bash
export AIRFLOW_HOME="$PWD/ldp_demo/airflow/home"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export LDP_AIRFLOW_DEMO="$PWD/ldp_demo/airflow/data"
mkdir -p "$AIRFLOW_HOME/dags"
python examples/restaurant/airflow_dag.py --prepare "$LDP_AIRFLOW_DEMO"
cp examples/restaurant/airflow_dag.py "$AIRFLOW_HOME/dags/"
airflow standalone
```

The `--prepare` step writes the seven daily drops of the restaurant example, `orders_2026-08-01.csv` to `orders_2026-08-07.csv` with 30 orders in all, and a copy of the orders config set to read the drop of its window. Everything else in the work folder is made by the runs. Then the DAG file goes into the DAGs folder, and `airflow standalone` prints where to open the UI, which is `http://localhost:8080`. In Airflow 3 the generated password may not be printed. If it is not, it is in `$AIRFLOW_HOME/simple_auth_manager_passwords.json.generated`.

Leave that terminal running and open a second one in the root of the clone. Activate the same virtual environment in it, and export the same three variables.

**On Windows.** Airflow does not run on Windows itself. Use WSL2, and keep the clone, the virtual environment and `AIRFLOW_HOME` in your Linux home folder, because Airflow's own quick start asks for the Linux home and not for the Windows drive mounted under `/mnt`.

**Memory.** Airflow's installation guide asks for at least 4 GB of memory for Airflow, and the Docker quick start asks for 4 GB and ideally 8 GB for Docker. On a Mac with Python 3.12, `airflow standalone` with this DAG used about 0.7 GB at rest, summed over its 24 processes. Right after the seven-day catch-up below, when the DAG parsers and the task processes had imported LDP, pyarrow and pyiceberg, it used 1.4 GB over 18 processes in one run and 1.7 GB over 21 in another. These sums count the pages that processes share once for each process, so the real use is lower. The DAG sets `max_active_runs=1`, so one task runs at a time.

## The DAG

The DAG is `examples/restaurant/airflow_dag.py`. Its core is these lines.

```python
with DAG(
    dag_id="restaurant_orders",
    schedule=CronDataIntervalTimetable("@daily", timezone="UTC"),
    start_date=datetime(2026, 8, 1, tzinfo=timezone.utc),
    end_date=datetime(2026, 8, 7, tzinfo=timezone.utc),  # the start of the last data interval
    catchup=True,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=1)},
    tags=["ldp"],
) as dag:
    LdpRunOperator(task_id="orders", config=str(config_path(workdir)))
```

`LdpRunOperator(config=..., mode=None)` runs one config. `config` is the path of the config, and it has to be a path that the worker can read, so give an absolute one. `mode` overrides the write mode of the target, as `ldp run --mode` does. Both are templated, and every other argument is an argument of Airflow's `BaseOperator`, such as `retries`.

**The schedule is not `"@daily"`.** Airflow 3 turns a cron string into a trigger timetable unless `create_cron_data_intervals` is `True` in the `[scheduler]` section of its configuration. A run of a trigger timetable has a data interval that starts and ends at the same instant, and an empty interval is no window to load. `LdpRunOperator` fails such a run with a message that names the fix, and Airflow does not retry it. `CronDataIntervalTimetable("@daily", timezone="UTC")`, from `airflow.timetables.interval`, gives each run the day it covers. The empty interval was seen on Airflow 3.0.0, 3.1.7 and 3.3.2.

**The interval and the config have to agree.** `LdpRunOperator` takes the data interval as the window as it is, and LDP does not compare it with `metadata.window` of the config. A timetable of days for a config that loads days is what the example does. With a time zone other than UTC, give the timetable and the config the same one, `CronDataIntervalTimetable("@daily", timezone="Asia/Kolkata")` and `"window": {"size": "day", "timezone": "Asia/Kolkata"}`, because the config reads the date in `{window_start}` and the rows of a timestamp column that has no time zone in the time zone of its window. Two intervals that overlap load their common rows twice, because each one has a key of its own.

**The run ids name the end of the interval.** The run `scheduled__2026-08-05T00:00:00+00:00` is the one for the day that ends on 5 August, so it loads the drop of 4 August. Its logical date is 4 August, the start of the interval, and that is the date that a backfill takes.

## Catch up a week, with one late drop

The retry is the part of Airflow that most needs the fence, so this run makes one drop late. Before you unpause the DAG, hide the drop of 4 August, as if the restaurant system had not exported it yet.

```bash
mv "$LDP_AIRFLOW_DEMO/landing/orders_2026-08-04.csv" "$LDP_AIRFLOW_DEMO/late.csv"
airflow dags unpause restaurant_orders
```

With `catchup=True` and a start date in the past, the scheduler makes one run for each missing day, and `max_active_runs=1` makes them run one after the other. Open the DAG in the UI and watch the grid. The first three days load within seconds. The run `scheduled__2026-08-05T00:00:00+00:00` is the one for 4 August. Its first try fails, because the file of its day is not there. That is a `FileNotFoundError`, an error that another try may fix, so the operator raises it as it is and Airflow retries the task after the minute of `retry_delay`.

Put the drop back after the task has failed once or twice. If the task has failed three times first, clear it in the UI and the next try loads the day.

```bash
mv "$LDP_AIRFLOW_DEMO/late.csv" "$LDP_AIRFLOW_DEMO/landing/orders_2026-08-04.csv"
```

The try that follows loads the day, and the runs after it go on. The whole catch-up takes about two and a half minutes, most of it the waits of the retry. Then read the table that the runs filled.

```bash
ldp commits "$LDP_AIRFLOW_DEMO/config/orders.json"
```

It lists one publish for each day. These are the columns that matter, with the drop that each loaded. The late drop was loaded by the third try, after two tries that wrote nothing.

| Run id (after `airflow:restaurant_orders:`) | Attempt | Rows | Drop |
|---|---|---|---|
| `scheduled__2026-08-02T00:00:00+00:00:orders` | 1 | 3 | 1 August |
| `scheduled__2026-08-03T00:00:00+00:00:orders` | 1 | 4 | 2 August |
| `scheduled__2026-08-04T00:00:00+00:00:orders` | 1 | 7 | 3 August |
| `scheduled__2026-08-05T00:00:00+00:00:orders` | 3 | 7 | 4 August |
| `scheduled__2026-08-06T00:00:00+00:00:orders` | 1 | 1 | 5 August |
| `scheduled__2026-08-07T00:00:00+00:00:orders` | 1 | 3 | 6 August |
| `scheduled__2026-08-08T00:00:00+00:00:orders` | 1 | 5 | 7 August |

The rows add up to 30, the size of the seven drops. The late day has the attempt 3 under the same run id as its two failed tries, because `ti.try_number` is the attempt and the run id is the same for every try. If a try had been lost after its commit, the next one would have found the key and written nothing, and a try that Airflow gave up on but that was still running could not publish after a later try had committed, because the first commit of that try fences it.

The tries that wrote nothing are not in `ldp commits`. The config has the `jsonl` sink, which writes its run events next to the config, and `ldp runs --attempts` lists every try from them.

```bash
ldp runs "$LDP_AIRFLOW_DEMO/config/.ldp/events.jsonl" --attempts
```

For the late day it lists three rows, with the same key `11044a7508f8` in all of them.

| Run id (after `airflow:restaurant_orders:`) | Attempt | Status | Rows written |
|---|---|---|---|
| `scheduled__2026-08-05T00:00:00+00:00:orders` | 1 | failed | |
| `scheduled__2026-08-05T00:00:00+00:00:orders` | 2 | failed | |
| `scheduled__2026-08-05T00:00:00+00:00:orders` | 3 | published | 7 |

The key of each publish is the key that `ldp plan` prints for that day.

```bash
ldp plan "$LDP_AIRFLOW_DEMO/config/orders.json" --window 2026-08-02/2026-08-03
```

Its `idempotency_key` line begins `eb361a7bf00b`, the key of the run `scheduled__2026-08-03T00:00:00+00:00`, which loaded the 4 orders of 2 August.

## Run a day again

Every run of a day that is in the table finds the key and writes nothing. To see it, ask Airflow to redo 2 August.

```bash
airflow backfill create --dag-id restaurant_orders \
    --from-date 2026-08-02 --to-date 2026-08-02 --reprocess-behavior completed
```

A backfill takes the logical dates of the days, which are the starts of the intervals, and both dates are inclusive. `--reprocess-behavior completed` makes Airflow clear the finished run of that day and run it again, as the next try of the same task. Without it, a day that has a successful run is left alone. The task succeeds, its return value says `skipped_duplicate` with 0 rows written, and `ldp commits` still has seven rows. `ldp runs --attempts` lists the second try of that run as `skipped_duplicate`. A DAG run of the same day under another run id finds the key in the same way.

`LdpRunOperator` has no replace option. To load a day again because its source changed, use `ldp backfill ... --replace`, which the [README](https://github.com/tusharchou/local-data-platform#backfill-a-range-of-windows) describes.

## What the task does with each result

| What happens | What the task does | Airflow |
|---|---|---|
| The window is published | Succeeds and returns the `PipelineResult` as a dict | The dict is the `return_value` XCom |
| The window was published before (`skipped_duplicate`) | Succeeds and returns the dict, with 0 rows written | The same |
| A data gate holds the run back (`DataQualityError`) | Fails with `AirflowFailException` | No retry, because the same source would be held back again |
| The config cannot run (`ConfigError`), such as a path that is not there, or the DAG run has no data interval | Fails with `AirflowFailException` | No retry |
| Any other error, such as a drop that has not landed, a commit that kept losing to other writers or a network error | Raises the error as it is | A retry for each of `retries`, as the next attempt |

The data gate and the config error follow the rule that `retries` of an `ldp` workflow follow, which never retries them. `AirflowFailException` is in `airflow.sdk.exceptions` from Airflow 3.2 and in `airflow.exceptions` before that, and the operator uses whichever it finds.

## From a TaskFlow task or a PythonOperator

The functions that the operator uses are public. `run(config, context)` runs a config for the task that `context` describes, and returns the `PipelineResult`. The error rule above is the operator's, so a function of your own says what it wants.

```python
from airflow.sdk import get_current_context, task
from local_data_platform.integrations.airflow import run

@task(retries=2)
def orders():
    return run("/data/restaurant/config/orders.json", get_current_context()).to_dict()
```

Here a data gate raises `DataQualityError`, which Airflow retries like any other error. Catch it and raise `AirflowFailException` when you want no retry. `window_of`, `run_id_of`, `attempt_of`, `run_options` and `key_for(config, context)`, which is the key without running anything, take the same `context`, and so does anything with the same keys, which is how the tests of this module run without Airflow.

## Without importing LDP, the BashOperator recipe

When LDP is not in the environment of Airflow, or cannot be, call `ldp run` from a BashOperator. The recipe only needs the path of an `ldp` command that has the config's dependencies, so it works in an Airflow that is older than 3.1.7 as well.

```python
from datetime import datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from airflow.timetables.interval import CronDataIntervalTimetable

LDP = "/opt/ldp/bin/ldp"  # the ldp command of the virtual environment that has local-data-platform
CONFIG = "/data/restaurant/config/orders.json"

with DAG("restaurant_orders_bash", schedule=CronDataIntervalTimetable("@daily", timezone="UTC"),
         start_date=datetime(2026, 8, 1), catchup=True):
    BashOperator(
        task_id="orders",
        bash_command=(
            f"{LDP} run {CONFIG} "
            "--window '{{ data_interval_start }}/{{ data_interval_end }}' "
            "--run-id 'airflow:{{ ti.dag_id }}:{{ run_id }}:{{ ti.task_id }}' "
            "--attempt {{ ti.try_number }}"
        ),
        retries=2,
        do_xcom_push=False,
    )
```

The three flags are the three values of the table at the top. Airflow 3.3.2 writes a data interval in this command as `2026-08-02 00:00:00+00:00`, and `--window` reads that as it reads `2026-08-02T00:00:00+00:00`, so either form works. The run id is built as `LdpRunOperator` builds it, so a task can move from the BashOperator to the operator, or back, and still find what the other one published. A day was run with each of them, and the second one skipped it.

Three things are different from the operator.

- **Exit status 3 is retried.** `ldp run` exits with 0 for a published window and for a skipped duplicate, with 1 for an error and with 3 when a data gate held the run back. A BashOperator treats every status that is not 0 and not its skip code, 99, as a failure and retries it, so a blocked day is tried `retries` more times before the task fails. Keep `retries` small, or use `LdpRunOperator`, which fails a blocked day at once.
- **There is no XCom.** `ldp run --json` prints a result over several lines, and a BashOperator would push only the last one, so the recipe sets `do_xcom_push=False`. The log of the task has the text that `ldp run` printed.
- **The config and the `ldp` command are paths on the worker.** Both have to exist where the task runs.

## When something goes wrong

| What you see | Why | What to do |
|---|---|---|
| `the data interval of the DAG run ... is empty` | The schedule is a cron string, so the runs have no interval | Give the DAG a `CronDataIntervalTimetable`, or set `create_cron_data_intervals` to `True` in `[scheduler]` |
| `the DAG run ... has no data interval` | The run was not made from a schedule or a backfill, such as a run that an asset event started | Run the task from a schedule, a backfill or `airflow dags test` |
| `LdpRunOperator needs Apache Airflow 3` | The module was used in an environment that has no Airflow 3 | Install Airflow 3 in that environment, or use the BashOperator recipe |
| `pip` reports a conflict on pyarrow, pyiceberg or SQLAlchemy | The constraints file of that Airflow release does not accept LDP | Use Airflow 3.1.7 or newer, or keep LDP in its own environment |
| `config file not found` | The path of the config is wrong on the worker | Give an absolute path that the worker can read |
| A manual run stays `queued` | The DAG is paused, and Airflow queues the runs of a paused DAG | Unpause it |
| The catch-up stops at a day and the scheduler log repeats `run already exists; skipping dagrun creation` | A manual run, such as the one `airflow dags test restaurant_orders 2026-08-03` makes, holds the logical date of a day that the scheduler has not run yet | Do not run `airflow dags test` in an `AIRFLOW_HOME` that a scheduler uses, and start again in a clean one |
| A manual trigger loaded the wrong day | A trigger takes its data interval from the moment it is made, and not from the logical date that you give it | Use a backfill, or the schedule, to run a given day |

The last two rows are what Airflow 3.3.2 did. `airflow dags trigger` with a logical date of 4 June gave a run whose data interval was the last complete day before the moment of the trigger.

## What is not here

`LdpRunOperator` runs one config. DAGs that are built from workflow files, an `LdpTaskOperator`, an opt-in Airflow CI workflow and recipes for managed Airflow services are planned for the 0.2.0 milestone in the [roadmap](roadmap.md).

## What to read next

- [Exactly-once writes](exactly_once.md) explains the key, the run id, the attempt and the fence that this guide leans on.
- The [contract](design/v0_1_1.md#airflow-013) lists the rules of the operator and its mapping.
- `ldp schedule tick` runs windows without Airflow, with cron or launchd, and `ldp backfill` loads a range of windows. The [orchestration guide](orchestration.md) walks through both on the same restaurant example.
