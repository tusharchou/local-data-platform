"""A one-task Airflow 3 DAG that loads the daily order drops of the restaurant example, one data interval at a time.

The restaurant system drops one orders file a day, ``orders_2026-08-01.csv`` to ``orders_2026-08-07.csv``. The task is
an ``LdpRunOperator``. It loads the drop of its data interval into the raw orders table, by the idempotency key of that
interval, so Airflow can retry a task, catch up a week, or run a day again, and the day is loaded once. The guide is
``docs/airflow.md``. To try it, make the work folder first, from the repo::

    export LDP_AIRFLOW_DEMO="$PWD/ldp_demo/airflow/data"
    python examples/restaurant/airflow_dag.py --prepare "$LDP_AIRFLOW_DEMO"

Then start Airflow with this file in its DAGs folder, and with the same variable set::

    airflow standalone

In a second terminal with the same variables, ``airflow dags unpause restaurant_orders`` catches up all seven days, one
after the other, and ``ldp commits "$LDP_AIRFLOW_DEMO/config/orders.json"`` lists one publish for each of them.

The schedule is a ``CronDataIntervalTimetable`` and not the string ``"@daily"``. Airflow 3 gives a cron string a trigger
timetable by default, and its runs have a data interval that starts and ends at the same instant, which is no window to
load. ``LdpRunOperator`` fails such a run and says so.
"""

import argparse
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKDIR = Path(os.environ.get("LDP_AIRFLOW_DEMO", "~/ldp_airflow_demo")).expanduser()
ORDERS_PATH = "../landing/orders_{window_start:%Y-%m-%d}.csv"
"""The source path of the orders config, with the day of the window in it. The ``..`` is the work folder."""


def config_path(workdir: str | Path) -> Path:
    """Where the windowed orders config is in a work folder."""
    return Path(workdir) / "config" / "orders.json"


def _load_generator():
    spec = importlib.util.spec_from_file_location("restaurant_generate", HERE / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def windowed_orders_config() -> dict:
    """The orders config of the example, set to read the drop of one day and to keep the orders of that day.

    The checks and the target are the ones of ``config/orders.json``, so a load by Airflow is held to what the rest of
    the example holds a load to.
    """
    spec = json.loads((HERE / "config" / "orders.json").read_text())
    spec["what"] = "the orders of one day, as exported, added to the raw orders"
    spec["where"] = "landing/orders_<day>.csv"
    spec["metadata"]["source"]["path"] = ORDERS_PATH
    spec["metadata"]["window"] = {"size": "day", "column": "order_ts"}
    return spec


def prepare(workdir: str | Path) -> Path:
    """Write the seven daily drops and the windowed orders config into ``workdir``, and return the config path.

    It writes only these files, and replaces them if they are there, so it is safe to run again. The table that the
    runs fill is ``workdir/warehouse``.
    """
    generate = _load_generator()
    dataset = generate.generate()
    landing = Path(workdir) / "landing"
    landing.mkdir(parents=True, exist_ok=True)
    for day in generate.DROP_DAYS:
        generate.write_batch(landing, dataset, day.isoformat(), f"orders_{day}.csv", f"order_lines_{day}.csv")
    config = config_path(workdir)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(windowed_orders_config(), indent=2) + "\n")
    return config


def build_dag(workdir: str | Path):
    """The DAG, with one ``LdpRunOperator`` for the orders config in ``workdir``. Airflow is imported here."""
    from airflow.sdk import DAG
    from airflow.timetables.interval import CronDataIntervalTimetable

    from local_data_platform.integrations.airflow import LdpRunOperator

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
    return dag


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make the work folder of the Airflow example DAG: the daily drops of "
                                                 "the restaurant example and the windowed orders config.")
    parser.add_argument("--prepare", metavar="DIR", required=True, help="the work folder, made if it is not there")
    args = parser.parse_args(argv)
    config = prepare(args.prepare)
    print(f"Wrote the seven daily drops and the orders config to {args.prepare}. Next, tell Airflow where they are:\n"
          f"  export LDP_AIRFLOW_DEMO={Path(args.prepare).expanduser().resolve()}\n"
          "then start Airflow with this file in its DAGs folder and unpause restaurant_orders, as docs/airflow.md "
          f"shows.\nThe config is {config}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

dag = build_dag(WORKDIR)
