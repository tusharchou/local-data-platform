{{ config(
    materialized='incremental',
    unique_key='day',
    meta={'ldp_publish': true, 'ldp_namespace': 'gold', 'owner': 'shop analytics', 'grain': 'one row per day'}
) }}

-- One row per day, built for the window of the workflow run. The run passes the window as the variables
-- window_start and window_end, so the model reads only the orders of that window.
select
    cast(date_trunc('day', ordered_at) as date) as day,
    count(*) as orders,
    sum(amount) as revenue
from {{ ref('stg_orders') }}
where ordered_at >= '{{ var("window_start") }}'::timestamp
  and ordered_at < '{{ var("window_end") }}'::timestamp
group by 1
