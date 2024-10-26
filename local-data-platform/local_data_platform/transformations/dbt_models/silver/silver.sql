-- silver layer
with silver_data as (
    select * from {{ ref('bronze_to_silver') }}
)
select 
    neighbourhood_group, 
    avg(price) as avg_price, 
    count(id) as total_listings
from silver_data
group by neighbourhood_group
