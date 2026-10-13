{{ config(materialized='view') }}

select id as order_id, label, amount
from {{ source('raw', 'orders') }}
