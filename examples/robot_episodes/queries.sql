-- DuckDB research queries for the humanoid robot-episode example. The gold tables are built from the SQL files of
-- models/ by ldp gold build.
--
-- run.py registers every Iceberg table of the robots namespace under its own name (bronze_episodes,
-- bronze_frames, silver_episodes, silver_frames, gold_episode_stats, gold_robot_daily) and the
-- pinned training split as train_split and train_frames, then runs these queries by name.
-- Each query starts with a "-- name: <name>" line. The session time zone is UTC.

-- name: success_by_task
-- Which tasks are hard? Success rate over every promoted episode.
SELECT
    task,
    count(*) AS episodes,
    round(avg(CASE WHEN success THEN 1.0 ELSE 0.0 END), 3) AS success_rate
FROM gold_episode_stats
GROUP BY task
ORDER BY success_rate, task;

-- name: success_by_robot
-- Which robots need maintenance? h05's worn gripper drags its success rate down.
SELECT
    robot_id,
    count(*) AS episodes,
    round(avg(CASE WHEN success THEN 1.0 ELSE 0.0 END), 3) AS success_rate,
    round(avg(duration_s), 1) AS mean_duration_s
FROM gold_episode_stats
GROUP BY robot_id
ORDER BY success_rate, robot_id;

-- name: frame_drop_by_robot
-- Frame-drop rate per robot and upload batch, over everything recorded (bronze, including the
-- quarantined batch): h06's loose camera cable is plain to see in b02.
PIVOT (
    SELECT e.robot_id, f.batch_id, CASE WHEN f.dropped THEN 1.0 ELSE 0.0 END AS dropped
    FROM bronze_frames AS f
    JOIN bronze_episodes AS e USING (episode_id)
)
ON batch_id
USING round(avg(dropped), 4)
GROUP BY robot_id
ORDER BY robot_id;

-- name: clips_in_sync
-- Clips whose RGB and depth cameras stayed within 20 ms of each other on every frame, per robot,
-- over every recorded clip (bronze). h03's drifting depth clock in b02 breaks sync.
WITH clip AS (
    SELECT
        episode_id,
        max(abs(epoch_us(depth_ts) - epoch_us(rgb_ts))) / 1000.0 AS max_skew_ms
    FROM bronze_frames
    GROUP BY episode_id
)
SELECT
    e.robot_id,
    count(*) AS clips,
    count(*) FILTER (WHERE c.max_skew_ms < 20) AS clips_in_sync,
    round(avg(CASE WHEN c.max_skew_ms < 20 THEN 1.0 ELSE 0.0 END), 3) AS share_in_sync,
    round(max(c.max_skew_ms), 1) AS worst_skew_ms
FROM clip AS c
JOIN bronze_episodes AS e USING (episode_id)
GROUP BY e.robot_id
ORDER BY share_in_sync, e.robot_id;

-- name: training_split
-- What the pinned training split holds: clean, successful episodes, robot h08 held out.
SELECT
    t.task,
    count(DISTINCT t.episode_id) AS episodes,
    count(*) AS frames,
    round(count(*) / 30.0 / 60.0, 1) AS minutes_of_demonstration
FROM train_split AS t
JOIN train_frames AS f USING (episode_id)
GROUP BY t.task
ORDER BY episodes DESC, t.task;
