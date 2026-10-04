"""Deterministic synthetic humanoid-robot episodes and sensor frames.

A fleet of eight humanoids (``h01`` to ``h08``) records teleoperated demonstrations. Each
*episode* is one attempt at a task; each *frame* is one 30 Hz tick with an RGB image, a depth
image and the 28 joint positions. The generator writes one upload batch as two Parquet files,
the way a robot's uploader would drop them into a landing zone.

A *faulty* batch carries the problems that ruin a training set, each on a known robot:

| Fault | Where | Caught by |
|---|---|---|
| Loose camera cable: 15% of frames dropped | every ``h06`` episode | ``rate_below(dropped)`` |
| Depth camera clock drift: skew grows to ~60 ms | every ``h03`` episode | ``max_skew(rgb_ts, depth_ts)`` |
| NTP step: the clock jumps back 250 ms | the first ``h07`` episode | ``monotonic(rgb_ts by episode_id)`` |
| Recorder stall: 15 frames missing | the second ``h02`` episode | ``max_gap(rgb_ts by episode_id)`` |
| Unknown task label ``dance`` | the third ``h04`` episode | ``accepted_values(task)`` |

The same ``seed`` and batch id always give byte-identical tables. Nothing here needs the
network or a random-number library beyond :mod:`random`.

Run it on its own to look at a batch::

    python examples/robot_episodes/generate.py --out /tmp/landing --batch b02 --faulty
"""

import argparse
import datetime as dt
import math
import random
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROBOTS = tuple(f"h{i:02d}" for i in range(1, 9))
TASKS = ("pick_place", "open_door", "fold_towel", "pour_water", "stack_blocks", "wipe_table")
OPERATORS = ("op_ana", "op_ben", "op_chen", "op_dia", "op_eli")
JOINTS = 28  # 2 x 7 arm joints, 2 x 6 leg joints, 2 neck joints
FPS = 30
FRAME_US = 1_000_000 // FPS
URI_ROOT = "s3://humanoid-fleet-raw"

# How often each task succeeds, before operator skill and robot wear.
TASK_SUCCESS = {"pick_place": 0.92, "open_door": 0.78, "fold_towel": 0.58, "pour_water": 0.72,
                "stack_blocks": 0.82, "wipe_table": 0.88}
OPERATOR_SKILL = {"op_ana": 0.05, "op_ben": 0.0, "op_chen": -0.04, "op_dia": 0.03, "op_eli": -0.08}
# h05 has a worn gripper: it struggles with deformables and liquids.
ROBOT_WEAR = {("h05", "fold_towel"): -0.3, ("h05", "pour_water"): -0.25}

HEALTHY_DROP_RATE = 0.003
FAULT_DROP_RATE = 0.15
UTC = dt.timezone.utc

EPISODE_SCHEMA = pa.schema([
    ("batch_id", pa.string()),
    ("episode_id", pa.string()),
    ("robot_id", pa.string()),
    ("task", pa.string()),
    ("operator", pa.string()),
    ("start_ts", pa.timestamp("us", tz="UTC")),
    ("end_ts", pa.timestamp("us", tz="UTC")),
    ("success", pa.bool_()),
    ("frame_count", pa.int32()),
])
FRAME_SCHEMA = pa.schema([
    ("batch_id", pa.string()),
    ("episode_id", pa.string()),
    ("frame_idx", pa.int32()),
    ("rgb_ts", pa.timestamp("us", tz="UTC")),
    ("depth_ts", pa.timestamp("us", tz="UTC")),
    ("joint_state", pa.list_(pa.float64())),
    ("rgb_uri", pa.string()),
    ("depth_uri", pa.string()),
    ("dropped", pa.bool_()),
])


def _epoch_us(value: dt.datetime) -> int:
    return int((value - dt.datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()) * 1_000_000 + value.microsecond


def generate_batch(batch_id: str, day: dt.date, *, seed: int = 7, episodes_per_robot: int = 6,
                   faulty: bool = False) -> tuple[pa.Table, pa.Table]:
    """Generate one upload batch.

    Args:
        batch_id: The upload id, e.g. ``"b01"``. It prefixes every episode id.
        day: The recording day; episodes start from 08:00 UTC.
        seed: The random seed. The batch's random stream is seeded with ``f"{seed}:{batch_id}"``.
        episodes_per_robot: Episodes each of the eight robots records.
        faulty: Inject the faults listed in the module docstring.

    Returns:
        ``(episodes, frames)`` as pyarrow tables with :data:`EPISODE_SCHEMA` and :data:`FRAME_SCHEMA`.
    """
    rng = random.Random(f"{seed}:{batch_id}")
    episode_rows: dict[str, list] = {name: [] for name in EPISODE_SCHEMA.names}
    frame_rows: dict[str, list] = {name: [] for name in FRAME_SCHEMA.names}
    day_start = dt.datetime.combine(day, dt.time(8, 0), tzinfo=UTC)
    phases = [rng.uniform(0, 2 * math.pi) for _ in range(JOINTS)]
    speeds = [rng.uniform(0.2, 1.2) for _ in range(JOINTS)]
    amplitudes = [rng.uniform(0.1, 1.4) for _ in range(JOINTS)]

    for robot_number, robot in enumerate(ROBOTS):
        for slot in range(episodes_per_robot):
            episode_id = f"{batch_id}-{robot}-{slot:02d}"
            task = rng.choice(TASKS)
            operator = rng.choice(OPERATORS)
            if faulty and robot == "h04" and slot == 2:
                task = "dance"
            start = day_start + dt.timedelta(minutes=12 * slot, seconds=37 * robot_number + rng.randint(0, 20))
            count = rng.randint(3 * FPS, 6 * FPS)
            p_success = TASK_SUCCESS.get(task, 0.5) + OPERATOR_SKILL[operator] + ROBOT_WEAR.get((robot, task), 0.0)
            success = rng.random() < min(max(p_success, 0.02), 0.98)

            drop_rate = FAULT_DROP_RATE if faulty and robot == "h06" else HEALTHY_DROP_RATE
            drift_us_per_s = 12_000 if faulty and robot == "h03" else 0
            clock_step = (60, -250_000) if faulty and robot == "h07" and slot == 0 else None
            stall = range(40, 55) if faulty and robot == "h02" and slot == 1 else range(0)
            start_us = _epoch_us(start)
            offset = rng.uniform(0, 2 * math.pi)
            for idx in range(count):
                jitter = rng.randint(-1_500, 1_500)
                rgb_us = start_us + idx * FRAME_US + jitter
                if clock_step is not None and idx >= clock_step[0]:
                    rgb_us += clock_step[1]
                skew = min(max(rng.gauss(4_000, 2_000), 0), 12_000) + drift_us_per_s * idx / FPS
                depth_us = rgb_us + int(skew)
                dropped = rng.random() < drop_rate
                t = idx / FPS
                joints = [round(amplitudes[j] * math.sin(speeds[j] * t + phases[j] + offset)
                                + rng.gauss(0, 0.01), 4) for j in range(JOINTS)]
                if idx in stall:
                    continue  # the recorder stalled: these frames were never written
                frame_rows["batch_id"].append(batch_id)
                frame_rows["episode_id"].append(episode_id)
                frame_rows["frame_idx"].append(idx)
                frame_rows["rgb_ts"].append(rgb_us)
                frame_rows["depth_ts"].append(depth_us)
                frame_rows["joint_state"].append(joints)
                frame_rows["rgb_uri"].append(None if dropped else f"{URI_ROOT}/{episode_id}/rgb/{idx:05d}.jpg")
                frame_rows["depth_uri"].append(None if dropped else f"{URI_ROOT}/{episode_id}/depth/{idx:05d}.png")
                frame_rows["dropped"].append(dropped)

            episode_rows["batch_id"].append(batch_id)
            episode_rows["episode_id"].append(episode_id)
            episode_rows["robot_id"].append(robot)
            episode_rows["task"].append(task)
            episode_rows["operator"].append(operator)
            episode_rows["start_ts"].append(start_us)
            episode_rows["end_ts"].append(start_us + count * FRAME_US)
            episode_rows["success"].append(success)
            episode_rows["frame_count"].append(count)

    return pa.table(episode_rows, schema=EPISODE_SCHEMA), pa.table(frame_rows, schema=FRAME_SCHEMA)


def write_batch(landing: str | Path, episodes: pa.Table, frames: pa.Table) -> tuple[Path, Path]:
    """Write a batch to ``<landing>/episodes.parquet`` and ``<landing>/frames.parquet``, replacing the last one.

    Returns:
        The two paths.
    """
    landing = Path(landing)
    landing.mkdir(parents=True, exist_ok=True)
    paths = landing / "episodes.parquet", landing / "frames.parquet"
    pq.write_table(episodes, paths[0])
    pq.write_table(frames, paths[1])
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write one synthetic humanoid-robot upload batch as Parquet.")
    parser.add_argument("--out", required=True, help="landing folder for episodes.parquet and frames.parquet")
    parser.add_argument("--batch", default="b01", help="batch id (default b01)")
    parser.add_argument("--day", default="2026-09-01", help="recording day, YYYY-MM-DD (default 2026-09-01)")
    parser.add_argument("--seed", type=int, default=7, help="random seed (default 7)")
    parser.add_argument("--episodes-per-robot", type=int, default=6, help="default 6")
    parser.add_argument("--faulty", action="store_true",
                        help="inject drops, skew, a clock step, a stall and a bad label")
    args = parser.parse_args(argv)
    episodes, frames = generate_batch(args.batch, dt.date.fromisoformat(args.day), seed=args.seed,
                                      episodes_per_robot=args.episodes_per_robot, faulty=args.faulty)
    paths = write_batch(args.out, episodes, frames)
    print(f"Wrote {episodes.num_rows} episodes to {paths[0]} and {frames.num_rows} frames to {paths[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
