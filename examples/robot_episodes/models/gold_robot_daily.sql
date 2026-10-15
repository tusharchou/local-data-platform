-- The gold table gold_robot_daily, built by ldp gold build from gold_episode_stats, which is its only input,
-- so it is built after it. The session time zone is UTC.
-- Fleet health per robot per day.
SELECT
    robot_id,
    CAST(start_ts AS DATE) AS recorded_on,
    count(*) AS episodes,
    count(*) FILTER (WHERE success) AS successes,
    avg(CASE WHEN success THEN 1.0 ELSE 0.0 END) AS success_rate,
    CAST(sum(frames_recorded) AS BIGINT) AS frames,
    CAST(sum(dropped_frames) AS BIGINT) AS dropped_frames,
    sum(dropped_frames) / sum(frames_recorded) AS drop_rate,
    max(max_skew_ms) AS max_skew_ms
FROM gold_episode_stats
GROUP BY ALL
ORDER BY robot_id, recorded_on;
