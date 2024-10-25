-- silver/bronze_to_silver.sql
with bronze_data as (
    select * from {{ source('bronze', 'airbnb_flat') }}
)
select 
    id, name, host_id, neighbourhood_group, neighbourhood, 
    room_type, price, minimum_nights, availability_365
from bronze_data
where price is not null
