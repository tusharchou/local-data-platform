-- The gold table gold_episode_stats, built by ldp gold build from the tables of its inputs, which are
-- silver_episodes.json and silver_frames.json. Each input is registered under the name of its table.
-- The session time zone is UTC.
-- One row per promoted episode: outcome, frame health and camera sync. A clip is "clean" when it
-- dropped under 2% of its frames and its RGB and depth cameras never drifted 20 ms apart.
SELECT
    e.episode_id,
    e.batch_id,
    e.robot_id,
    e.task,
    e.operator,
    e.start_ts,
    e.end_ts,
    e.duration_s,
    e.success,
    e.frame_count,
    count(*) AS frames_recorded,
    count(*) FILTER (WHERE f.dropped) AS dropped_frames,
    avg(CASE WHEN f.dropped THEN 1.0 ELSE 0.0 END) AS drop_rate,
    max(abs(f.skew_ms)) AS max_skew_ms,
    quantile_cont(abs(f.skew_ms), 0.95) AS p95_skew_ms,
    (avg(CASE WHEN f.dropped THEN 1.0 ELSE 0.0 END) < 0.02 AND max(abs(f.skew_ms)) < 20) AS clean
FROM silver_episodes AS e
JOIN silver_frames AS f USING (episode_id)
GROUP BY ALL
ORDER BY e.episode_id;
