{{ config(
    materialized='incremental',
    unique_key='order_id',
    incremental_strategy='delete+insert',
    meta={'ldp_publish': true, 'ldp_namespace': 'silver'}
) }}

-- An order is new, or changed, when its updated_at is later than the newest one already here. The late June orders
-- of batch 2 have an order_ts in June, so a filter on order_ts would miss them. updated_at, when the restaurant
-- system last changed the order, does not.
select
    order_id,
    restaurant_id,
    customer_id,
    order_ts,
    channel,
    total_amount,
    updated_at
from {{ ref('stg_orders') }}
{% if is_incremental() %}
where updated_at > (select max(updated_at) from {{ this }})
{% endif %}
