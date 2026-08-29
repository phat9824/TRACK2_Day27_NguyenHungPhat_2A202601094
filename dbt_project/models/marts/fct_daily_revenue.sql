-- Daily completed-order revenue for the CEO dashboard.
--
-- The starter left-joined every active customer row. `stg_customers` is an SCD
-- dimension, so a customer whose old version is never closed out has two active
-- rows, the join fans out, and every order for that customer is counted twice --
-- with no SQL error, no failed run, and a revenue chart that simply goes up.
--
-- Fix: reduce the dimension to one row per customer *before* joining. Keeping the
-- join (rather than deleting it, which today's column list would allow) is
-- deliberate: the moment anyone selects a customer attribute here, the grain
-- guarantee has to already be in place.

with completed_orders as (
    select *
    from {{ ref('stg_orders') }}
    where status = 'completed'
),
active_customers as (
    select *
    from {{ ref('stg_customers') }}
    where is_active = true
),
current_customer as (
    select *
    from (
        select
            *,
            row_number() over (
                partition by customer_id
                order by valid_from desc nulls last
            ) as version_rank
        from active_customers
    )
    where version_rank = 1
)
select
    o.order_date,
    count(*) as completed_order_rows,
    sum(o.amount_usd) as daily_revenue
from completed_orders o
left join current_customer c
    on o.customer_id = c.customer_id
group by 1
order by 1
