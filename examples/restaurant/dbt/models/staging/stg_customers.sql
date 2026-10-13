select
    customer_id,
    name as customer_name,
    city,
    signed_up_at
from {{ source('raw', 'customers') }}
