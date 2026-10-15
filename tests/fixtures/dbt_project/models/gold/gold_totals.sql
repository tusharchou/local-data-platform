{{ config(materialized='table', meta={
    'ldp_publish': true, 'ldp_namespace': 'gold', 'owner': 'shop analytics', 'grain': 'one row for all orders'
}) }}

select count(*) as orders, sum(amount) as revenue
from {{ ref('fct_orders') }}
