{{ config(
    tags=['customers'],
    meta={
        'ldp_publish': true,
        'ldp_namespace': 'gold',
        'owner': 'customer insights',
        'grain': 'one row for each month',
        'ldp_quality': {
            'on_failure': 'fail',
            'checks': [
                {'check': 'row_count', 'min': 1},
                {'check': 'not_null', 'columns': ['month', 'customers_at_start', 'churned', 'active_customers']},
                {'check': 'unique', 'columns': ['month']},
                {'check': 'range', 'column': 'churn_rate', 'min': 0, 'max': 1}
            ]
        }
    }
) }}

-- Churn is counted in the month a customer stops ordering. For month M:
--   customers_at_start  customers with an order in month M minus 1
--   churned             those of them with no order in month M
--   churn_rate          churned divided by customers_at_start, or null when customers_at_start is 0
--   active_customers    customers with an order in month M
-- The first month has no row, because there is no month before it. The months run from the first to the last month
-- with an order, so a month in which nobody ordered still has a row, with no active customers. The metrics churn_rate
-- and active_customers of examples/restaurant/config/metrics.json, and the insights of 0.1.7, use this convention.
with activity as (
    select * from {{ ref('customer_activity_monthly') }}
),

bounds as (
    select min(month) as first_month, max(month) as last_month from activity
),

months as (
    select cast(month as date) as month
    from bounds, generate_series(bounds.first_month, bounds.last_month, interval 1 month) as series(month)
),

active as (
    select month, count(distinct customer_id) as active_customers
    from activity
    group by month
),

churn as (
    select
        months.month,
        count(previous.customer_id) as customers_at_start,
        count(previous.customer_id) filter (where current.customer_id is null) as churned
    from months
    left join activity as previous
        on previous.month = cast(months.month - interval 1 month as date)
    left join activity as current
        on current.customer_id = previous.customer_id and current.month = months.month
    where months.month > (select first_month from bounds)
    group by months.month
)

select
    churn.month,
    customers_at_start,
    churned,
    case when customers_at_start = 0 then null else cast(churned as double) / customers_at_start end as churn_rate,
    coalesce(active_customers, 0) as active_customers
from churn
left join active on active.month = churn.month
