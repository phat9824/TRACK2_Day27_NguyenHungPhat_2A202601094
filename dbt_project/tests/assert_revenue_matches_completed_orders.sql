-- Singular business test: the mart must not create or lose money.
--
-- `not_null` and `unique` cannot see this class of bug. A fan-out from the
-- customer join keeps every column valid and every key unique while quietly
-- doubling the totals; the only thing that changes is the number itself. So the
-- assertion is stated against the source of truth: total revenue in the mart
-- equals total completed revenue in staging, to the cent.
--
-- Returns zero rows when the assertion holds.

with mart as (
    select
        sum(daily_revenue) as revenue,
        sum(completed_order_rows) as order_rows
    from {{ ref('fct_daily_revenue') }}
),
source as (
    select
        sum(amount_usd) as revenue,
        count(*) as order_rows
    from {{ ref('stg_orders') }}
    where status = 'completed'
)
select
    mart.revenue as mart_revenue,
    source.revenue as source_revenue,
    mart.order_rows as mart_order_rows,
    source.order_rows as source_order_rows
from mart
cross join source
where abs(coalesce(mart.revenue, 0) - coalesce(source.revenue, 0)) > 0.01
   or coalesce(mart.order_rows, 0) <> coalesce(source.order_rows, 0)
