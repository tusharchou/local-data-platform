{{ config(
    tags=['sales'],
    meta={
        'ldp_publish': true,
        'ldp_namespace': 'gold',
        'owner': 'restaurant operations',
        'grain': 'one row for each restaurant on each day it had an order',
        'ldp_quality': {
            'on_failure': 'fail',
            'checks': [
                {'check': 'row_count', 'min': 1},
                {'check': 'not_null', 'columns': ['restaurant_day_id', 'day', 'restaurant_id', 'orders', 'revenue']},
                {'check': 'unique', 'columns': ['restaurant_day_id']},
                {'check': 'range', 'column': 'orders', 'min': 1},
                {'check': 'range', 'column': 'revenue', 'min': 0}
            ]
        }
    }
) }}

-- Orders, revenue and the average ticket for each restaurant on each day it had an order. The average ticket is the
-- revenue divided by the orders. The metrics revenue, orders and avg_ticket of config/metrics.json are made from
-- this table, and add up the days with sums, so that the average ticket of a month is its revenue over its orders.
select
    orders.restaurant_id || ':' || cast(cast(orders.order_ts as date) as varchar) as restaurant_day_id,
    cast(orders.order_ts as date) as day,
    orders.restaurant_id,
    restaurants.restaurant_name,
    restaurants.city,
    restaurants.cuisine,
    count(*) as orders,
    round(sum(orders.total_amount), 2) as revenue,
    round(sum(orders.total_amount) / count(*), 2) as avg_ticket
from {{ ref('fct_orders') }} as orders
inner join {{ ref('stg_restaurants') }} as restaurants using (restaurant_id)
group by 1, 2, 3, 4, 5, 6
