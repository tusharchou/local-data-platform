-- A singular test: it passes when it returns no rows. A churn rate is a share, so it is never below 0 or above 1.
-- A null rate (a month that starts with no customers) is not a row here.
select month, churn_rate
from {{ ref('churn_by_month') }}
where churn_rate < 0 or churn_rate > 1
