{{ config(
    tags=['sales', 'ratings'],
    meta={
        'ldp_publish': true,
        'ldp_namespace': 'gold',
        'owner': 'restaurant operations',
        'grain': 'one row for each restaurant',
        'ldp_quality': {
            'on_failure': 'fail',
            'checks': [
                {'check': 'row_count', 'min': 1},
                {'check': 'not_null', 'columns': ['restaurant_id', 'as_of', 'orders_30d', 'revenue_30d', 'city_rank']},
                {'check': 'unique', 'columns': ['restaurant_id']},
                {'check': 'range', 'column': 'avg_rating', 'min': 1, 'max': 5},
                {'check': 'range', 'column': 'city_rank', 'min': 1}
            ]
        }
    }
) }}

-- A scorecard for each restaurant. as_of is the day of the newest order, and the orders and the revenue are those of the
-- 30 days that end on it, counting that day. The rating is the average of all the reviews of the restaurant, and
-- rating_sum and review_count are what that average is made of, so that the rating of several restaurants can be worked
-- out from them. city_rank is the rank of the restaurant by revenue among the restaurants of its city, where 1 is the
-- best, and two restaurants with the same revenue have the same rank.
with latest as (
    select max(cast(order_ts as date)) as as_of from {{ ref('fct_orders') }}
),

recent as (
    select
        orders.restaurant_id,
        count(*) as orders_30d,
        round(sum(orders.total_amount), 2) as revenue_30d
    from {{ ref('fct_orders') }} as orders
    cross join latest
    where cast(orders.order_ts as date) > latest.as_of - 30
    group by 1
),

ratings as (
    select
        restaurant_id,
        count(*) as review_count,
        cast(sum(rating) as bigint) as rating_sum,
        round(avg(rating), 2) as avg_rating
    from {{ ref('stg_reviews') }}
    group by 1
),

scored as (
    select
        restaurants.restaurant_id,
        restaurants.restaurant_name,
        restaurants.city,
        restaurants.cuisine,
        latest.as_of,
        coalesce(recent.orders_30d, 0) as orders_30d,
        coalesce(recent.revenue_30d, 0) as revenue_30d,
        coalesce(ratings.review_count, 0) as review_count,
        ratings.rating_sum,
        ratings.avg_rating
    from {{ ref('stg_restaurants') }} as restaurants
    cross join latest
    left join recent using (restaurant_id)
    left join ratings using (restaurant_id)
)

select
    *,
    rank() over (partition by city order by revenue_30d desc) as city_rank
from scored
