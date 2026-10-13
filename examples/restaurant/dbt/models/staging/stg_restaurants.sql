select
    restaurant_id,
    name as restaurant_name,
    location as city,
    cuisine,
    created_at
from {{ source('raw', 'restaurants') }}
