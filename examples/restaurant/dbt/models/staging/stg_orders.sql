-- The raw orders are append-only, so an order that was changed after it was loaded appears twice. This view keeps
-- every version, and fct_orders keeps the latest.
select
    order_id,
    restaurant_id,
    customer_id,
    order_ts,
    channel,
    total_amount,
    updated_at
from {{ source('raw', 'orders') }}
