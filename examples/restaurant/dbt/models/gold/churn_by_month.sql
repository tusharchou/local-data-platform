{{ config(meta={'ldp_publish': true, 'ldp_namespace': 'gold'}) }}

-- Churn is counted in the month a customer stops ordering. For month M:
--   customers_at_start  customers with an order in month M minus 1
--   churned             those of them with no order in month M
--   churn_rate          churned divided by customers_at_start, or null when customers_at_start is 0
-- The first month has no row, because there is no month before it. The months run from the first to the last month
-- with an order, so a month in which nobody ordered still has a row. The gold layer (0.1.4) and the insights (0.1.7)
-- reuse this convention.
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
    month,
    customers_at_start,
    churned,
    case when customers_at_start = 0 then null else cast(churned as double) / customers_at_start end as churn_rate
from churn
