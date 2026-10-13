{{ config(
    materialized='incremental',
    unique_key='order_id',
    meta={'ldp_publish': true, 'ldp_namespace': 'silver'}
) }}

select order_id, label, amount
from {{ ref('stg_orders') }}
{% if is_incremental() %}
where order_id > (select coalesce(max(order_id), 0) from {{ this }})
{% endif %}
