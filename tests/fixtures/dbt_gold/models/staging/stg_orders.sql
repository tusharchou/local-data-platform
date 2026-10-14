{{ config(materialized='view') }}

select id as order_id, cast("at" as timestamp) as ordered_at, amount
from {{ source('shop', 'orders') }}
