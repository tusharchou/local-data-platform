{{ config(
    tags=['menu'],
    meta={
        'ldp_publish': true,
        'ldp_namespace': 'gold',
        'owner': 'kitchen operations',
        'grain': 'one row for each menu item in each week in which it was ordered',
        'ldp_quality': {
            'on_failure': 'fail',
            'checks': [
                {'check': 'row_count', 'min': 1},
                {'check': 'not_null', 'columns': ['menu_item_week_id', 'week', 'menu_item_id', 'quantity', 'revenue']},
                {'check': 'unique', 'columns': ['menu_item_week_id']},
                {'check': 'range', 'column': 'quantity', 'min': 1},
                {'check': 'range', 'column': 'revenue', 'min': 0}
            ]
        }
    }
) }}

-- What each menu item sold in each week. A week starts on Monday. A line counts when the order that it belongs to is in
-- fct_orders, at the price it was sold for, and an item that nobody ordered in a week has no row for it.
select
    items.menu_item_id || ':' || cast(cast(date_trunc('week', orders.order_ts) as date) as varchar) as menu_item_week_id,
    cast(date_trunc('week', orders.order_ts) as date) as week,
    items.menu_item_id,
    items.item_name,
    items.category,
    items.restaurant_id,
    restaurants.restaurant_name,
    count(distinct lines.order_id) as orders,
    cast(sum(lines.quantity) as bigint) as quantity,
    round(sum(lines.quantity * lines.unit_price), 2) as revenue
from {{ ref('stg_order_lines') }} as lines
inner join {{ ref('fct_orders') }} as orders using (order_id)
inner join {{ ref('stg_menu_items') }} as items using (menu_item_id)
inner join {{ ref('stg_restaurants') }} as restaurants on restaurants.restaurant_id = items.restaurant_id
group by 1, 2, 3, 4, 5, 6, 7
