{# Custom generic data tests.

   A generic test is a parameterised query that must return zero rows. These two
   encode business invariants that `not_null` / `unique` cannot express. #}

{% test non_negative(model, column_name) %}
-- Revenue, counts and amounts are never negative. A negative value means a sign
-- flip upstream (refunds merged into sales, a currency conversion gone wrong),
-- which `not_null` happily lets through.
select {{ column_name }}
from {{ model }}
where {{ column_name }} < 0
{% endtest %}


{% test at_most_one_active_version(model, column_name) %}
-- SCD grain guard: at most one active row per business key. This is the exact
-- condition that makes the revenue join fan out, tested at its source instead of
-- being discovered downstream on the dashboard.
select
    {{ column_name }},
    count(*) as active_versions
from {{ model }}
where is_active = true
group by {{ column_name }}
having count(*) > 1
{% endtest %}
