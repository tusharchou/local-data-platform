{{ config(meta={'ldp_publish': true, 'ldp_namespace': 'gold'}) }}

-- Orders and revenue for each restaurant on each day it had an order.
select
    orders.restaurant_id || ':' || cast(cast(orders.order_ts as date) as varchar) as restaurant_day_id,
    orders.restaurant_id,
    restaurants.restaurant_name,
    cast(orders.order_ts as date) as day,
    count(*) as orders,
    round(sum(orders.total_amount), 2) as revenue
from {{ ref('fct_orders') }} as orders
inner join {{ ref('stg_restaurants') }} as restaurants using (restaurant_id)
group by 1, 2, 3, 4
