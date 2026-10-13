-- One row for each customer and each month in which they ordered. churn_by_month is built from it.
select
    customer_id || ':' || strftime(month, '%Y-%m') as customer_month_id,
    customer_id,
    month,
    orders,
    revenue
from (
    select
        customer_id,
        cast(date_trunc('month', order_ts) as date) as month,
        count(*) as orders,
        round(sum(total_amount), 2) as revenue
    from {{ ref('fct_orders') }}
    group by 1, 2
)
