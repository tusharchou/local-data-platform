select
    review_id,
    order_id,
    customer_id,
    restaurant_id,
    rating,
    comment,
    reviewed_at
from {{ source('raw', 'reviews') }}
