{{ config(materialized='table', meta={'ldp_publish': true, 'ldp_namespace': 'gold'}) }}

select count(*) as orders, sum(amount) as revenue
from {{ ref('fct_orders') }}
