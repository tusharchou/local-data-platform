{{ config(
    materialized='incremental',
    unique_key='order_id',
    incremental_strategy='delete+insert',
    meta={'ldp_publish': true, 'ldp_namespace': 'silver'}
) }}

-- The raw orders keep every version of an order, and this model keeps the latest. An incoming order is taken when
-- this table has no row for it, or only an older version. That compares each order with its own earlier row, so an
-- order that arrives late is taken whatever its order_ts or updated_at, and a newer version replaces its earlier
-- row (delete+insert on order_id). A filter on order_ts, or on the newest updated_at in this table, would skip
-- the late orders of batch 2, or a correction that reached the lake out of order.
-- When several versions of an order arrive together, rank() keeps the newest. Two rows with the same order_id and
-- the same updated_at are a duplicate that cannot be resolved, so both stay and the unique test fails the build.
select
    order_id,
    restaurant_id,
    customer_id,
    order_ts,
    channel,
    total_amount,
    updated_at
from {{ ref('stg_orders') }} as incoming
{% if is_incremental() %}
where not exists (
    select 1
    from {{ this }} as loaded
    where loaded.order_id = incoming.order_id and loaded.updated_at >= incoming.updated_at
)
{% endif %}
qualify rank() over (partition by order_id order by updated_at desc) = 1
