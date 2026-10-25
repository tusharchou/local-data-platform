select
    menu_item_id,
    restaurant_id,
    name as item_name,
    category,
    price
from {{ source('raw', 'menu_items') }}
