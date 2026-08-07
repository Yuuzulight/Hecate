-- A source that cannot report a signal must not be scored down for it.
--
-- Returns rows on failure. npm has no stars and GitHub no downloads, so if an
-- absent component fed the average as a zero, every npm package would sit below
-- every repository for a reason that has nothing to do with momentum.
--
-- The check: no row may have a momentum lower than its own worst measured
-- component. That can only happen if something absent was counted as zero.

with parts as (

    select
        repository_id,
        momentum,
        least(
            coalesce(growth_component, 999),
            coalesce(usage_component, 999),
            coalesce(attention_component, 999)
        ) as worst_measured
    from {{ ref('fct_momentum') }}
    where signals_measured > 0

)

select repository_id, momentum, worst_measured
from parts
where momentum < worst_measured - 0.01
