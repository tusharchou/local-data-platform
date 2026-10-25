-- The raw order lines are append-only, so a line that was loaded twice appears twice. order_line_id is the key that
-- tells them apart, and a test says that it is unique.
select
    order_id || ':' || cast(line_no as varchar) as order_line_id,
    order_id,
    line_no,
    menu_item_id,
    quantity,
    unit_price
from {{ source('raw', 'order_lines') }}
